"""API integration tests for crash ingestion, SSE, logs, sessions, and issue workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import event

from tests._api_integration_helpers import _drain, open_api_harness


@pytest.fixture
async def api_harness(tmp_path: Path):
    async with open_api_harness(tmp_path) as harness:
        yield harness


async def test_crash_status_scopes_project_keys_and_closes_db_before_sse_send(
    api_harness: Any,
) -> None:
    client = api_harness.client
    owner = (
        await client.post(
            "/api/v1/projects",
            json={"key": "owner", "name": "Owner", "settings_json": {"ai_triage_mode": "MANUAL"}},
        )
    ).json()
    other = (await client.post("/api/v1/projects", json={"key": "other", "name": "Other"})).json()
    own_key = {"X-API-Key": owner["api_key"]}
    uploaded = await client.post(
        "/api/v1/crashes/upload",
        auth=None,
        headers=own_key,
        data={"project_key": owner["key"], "app_version": "1.0.0"},
        files={"dump_file": ("sample.dmp", b"MDMP_TEST_FIXTURE")},
    )
    assert uploaded.status_code == 202
    crash_id = uploaded.json()["crash_id"]
    await _drain(api_harness)

    detail_url = f"/api/v1/crashes/{crash_id}"
    status_url = f"{detail_url}/status"
    event_url = f"{detail_url}/events"
    assert (await client.get(detail_url, auth=None, headers=own_key)).status_code == 401
    for path in (status_url, event_url):
        assert (await client.get(path, auth=None)).status_code == 401
        wrong_key = {"X-API-Key": other["api_key"]}
        assert (await client.get(path, auth=None, headers=wrong_key)).status_code == 403

    snapshot = await client.get(status_url, auth=None, headers=own_key)
    assert snapshot.status_code == 200
    assert snapshot.headers["cache-control"] == "no-store"
    assert snapshot.json() == {
        "crash_id": crash_id,
        "status": "ANALYZED",
        "ai_verdict": None,
        "error": None,
    }

    pool = api_harness.app.state.engine.sync_engine.pool
    checked_out_count = 0
    counts_during_send: list[int] = []

    @event.listens_for(pool, "checkout")
    def count_checkout(*_: Any) -> None:
        nonlocal checked_out_count
        checked_out_count += 1

    @event.listens_for(pool, "checkin")
    def count_checkin(*_: Any) -> None:
        nonlocal checked_out_count
        checked_out_count -= 1

    async def observe_send(scope: Any, receive: Any, send: Any) -> None:
        async def record(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                counts_during_send.append(checked_out_count)
            await send(message)

        await api_harness.app(scope, receive, record)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=observe_send), base_url="http://testserver"
    ) as machine_client:
        response = await machine_client.get(event_url, headers=own_key)
    assert response.status_code == 200
    assert '"status": "ANALYZED"' in response.text
    assert counts_during_send == [0]


async def test_crash_pipeline_sse_logs_verdict_and_issues_merge_split(api_harness: Any) -> None:
    client: httpx.AsyncClient = api_harness.client
    proj = (
        await client.post(
            "/api/v1/projects",
            json={"key": "crash-flow", "name": "Crash Flow"},
        )
    ).json()

    log_payload = (
        b"\xef\xbb\xbf2026-11-22 10:00:00.000 INFO startup ok\n"
        + "2026-11-22 10:00:05.000 ERROR ERR_RENDER_NULL 纹理为空\n".encode("gbk")
        + b"2026-11-22 10:00:08.000 FATAL aborting process\n"
    )
    api_harness.openai_client.queue_verdict(verdict="NEW_ISSUE", suggested_title="Core Render Null")

    up = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj["api_key"]},
        data={
            "project_key": proj["key"],
            "app_version": "2.0.0",
            "build_id": "b100",
            "os_version": "Windows 11",
            "crash_time": "2026-11-22T10:00:09Z",
        },
        files=[
            ("dump_file", ("crash.dmp", b"MDMP_VALID_HEADER_DATA", "application/octet-stream")),
            ("log_files", ("game.log", log_payload, "text/plain")),
        ],
    )
    assert up.status_code == 202
    crash_id = up.json()["crash_id"]
    log_id = up.json()["log_ids"][0]

    await _drain(api_harness)

    # Late-connect SSE on ANALYZED crash emits terminal snapshot
    async with client.stream("GET", f"/api/v1/crashes/{crash_id}/events") as sse_resp:
        assert sse_resp.status_code == 200
        body = (await sse_resp.aread()).decode("utf-8")
        assert "event: status" in body
        assert '"status": "ANALYZED"' in body

    detail = (await client.get(f"/api/v1/crashes/{crash_id}")).json()
    assert detail["status"] == "ANALYZED"
    assert detail["dump_path"] is not None
    assert detail["ai_verdict"] == "NEW_ISSUE"
    assert len(detail["ai_runs"]) == 1
    issue_id = detail["issue"]["id"]

    # Missing symbols queue handles debug_id=None safely without 500
    missing = await client.get("/api/v1/symbols/missing", params={"project_id": proj["id"]})
    assert missing.status_code == 200
    assert isinstance(missing.json(), list)
    assert any(m["debug_file"] == "NoDebugId.pdb" and m["debug_id"] == "" for m in missing.json())

    # Logs endpoints
    summary = (await client.get(f"/api/v1/logs/{log_id}/summary")).json()
    assert summary["total_lines"] == 3
    assert summary["digest"]["error_count"] >= 2

    lines = (
        await client.get(f"/api/v1/logs/{log_id}/lines", params={"start": 1, "end": 10})
    ).json()
    assert len(lines["lines"]) == 3
    assert "纹理为空" in lines["lines"][1]["text"]

    around = (
        await client.get(
            f"/api/v1/logs/{log_id}/around-time",
            params={"timestamp": "2026-11-22T10:00:05Z", "window_sec": 2},
        )
    ).json()
    assert around["window_sec"] == 2
    assert any("ERR_RENDER_NULL" in item["text"] for item in around["lines"])

    search = (
        await client.get(
            f"/api/v1/logs/{log_id}/search",
            params={"pattern": "ERR_.*NULL", "is_regex": "true", "max_matches": 200},
        )
    ).json()
    assert search["total_matches"] == 1
    assert (
        await client.get(
            f"/api/v1/logs/{log_id}/search",
            params={"pattern": "[bad", "is_regex": "true"},
        )
    ).status_code == 422

    # Artifacts
    dmp_dl = await client.get(f"/api/v1/crashes/{crash_id}/artifacts/dump")
    assert dmp_dl.status_code == 200
    assert dmp_dl.content == b"MDMP_VALID_HEADER_DATA"
    stacks_resp = await client.get(f"/api/v1/crashes/{crash_id}/artifacts/stacks_json")
    assert stacks_resp.status_code == 200

    # Human verdict KB feedback (confirm + create_issue)
    verdict_resp = await client.post(
        f"/api/v1/crashes/{crash_id}/verdict",
        json={
            "action": "confirm",
            "root_cause": "Verified GPU resource race.",
            "add_log_patterns": ["ERR_RENDER_NULL"],
        },
    )
    assert verdict_resp.status_code == 200
    assert verdict_resp.json()["is_human_verified"] is True
    assert verdict_resp.json()["issue"]["status"] == "KNOWN"

    # Manual issue + merge + invalid self-merge check
    manual_issue = (
        await client.post(
            "/api/v1/issues",
            json={"project_id": proj["id"], "title": "Merge Target", "status": "KNOWN"},
        )
    ).json()
    assert (
        await client.post(
            "/api/v1/issues/merge",
            json={"target_issue_id": manual_issue["id"], "source_issue_ids": [manual_issue["id"]]},
        )
    ).status_code == 400
    merged = (
        await client.post(
            "/api/v1/issues/merge",
            json={"target_issue_id": manual_issue["id"], "source_issue_ids": [issue_id]},
        )
    ).json()
    assert merged["crash_count"] == 1
    assert len(merged["clusters"]) == 1

    split_resp = await client.post(
        f"/api/v1/issues/{manual_issue['id']}/split",
        json={
            "cluster_id": merged["clusters"][0]["id"],
            "new_issue_title": "Split Out Cluster",
        },
    )
    assert split_resp.status_code == 200
    split_data = split_resp.json()
    assert split_data["source_issue"]["crash_count"] == 0
    assert split_data["target_issue"]["crash_count"] == 1
    assert split_data["target_issue"]["title"] == "Split Out Cluster"
    assert split_data["cluster"]["issue_id"] == split_data["target_issue"]["id"]

    # Corrupt dump -> FAILED + late-connect SSE includes non-empty error
    bad_up = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj["api_key"]},
        data={"project_key": proj["key"], "app_version": "2.0.0"},
        files={"dump_file": ("bad.dmp", b"CORRUPT_DUMP", "application/octet-stream")},
    )
    assert bad_up.status_code == 202
    bad_id = bad_up.json()["crash_id"]
    await _drain(api_harness)

    async with client.stream("GET", f"/api/v1/crashes/{bad_id}/events") as bad_sse:
        bad_body = (await bad_sse.aread()).decode("utf-8")
        assert '"status": "FAILED"' in bad_body
        data_line = [ln for ln in bad_body.splitlines() if ln.startswith("data:")][0]
        evt = json.loads(data_line[5:].strip())
        assert evt["error"]


async def test_crash_upload_sanitizes_log_filenames_and_cleans_partial_artifacts(
    api_harness: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.log_engine import LogEngine

    client: httpx.AsyncClient = api_harness.client
    proj = (
        await client.post(
            "/api/v1/projects",
            json={"key": "sanitize-proj", "name": "Sanitize Proj"},
        )
    ).json()

    # Conflicting X-API-Key header vs api_key form field -> 403
    conflict_resp = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj["api_key"]},
        data={
            "project_key": proj["key"],
            "app_version": "1.0.0",
            "api_key": "mismatched-form-key",
        },
        files={"dump_file": ("crash.dmp", b"MDMP_VALID", "application/octet-stream")},
    )
    assert conflict_resp.status_code == 403

    # Raw multipart payload with NUL byte and dot-dot traversal in log_files filenames
    boundary = "----CrashKillerRawBoundary98765"
    raw_multipart = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="project_key"\r\n\r\n{proj["key"]}\r\n'
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="app_version"\r\n\r\n1.0.0\r\n'
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="dump_file"; filename="crash.dmp"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
        "MDMP_VALID_HEADER_DATA\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="log_files"; filename="evil\x00.log"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "2026-11-22 10:00:00.000 INFO evil\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="log_files"; filename="../../.."\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "2026-11-22 10:00:01.000 INFO dotdot\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    up_resp = await client.post(
        "/api/v1/crashes/upload",
        headers={
            "X-API-Key": proj["api_key"],
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        content=raw_multipart,
    )
    assert up_resp.status_code == 202
    crash_id = up_resp.json()["crash_id"]
    await _drain(api_harness)

    detail = (await client.get(f"/api/v1/crashes/{crash_id}")).json()
    stored_names = [lg["filename"] for lg in detail["logs"]]
    assert len(stored_names) == 2
    for fname in stored_names:
        assert "\x00" not in fname
        assert fname not in {"", ".", ".."}
    assert "evil.log" in stored_names
    assert any(fname.endswith(".log") and fname != "evil.log" for fname in stored_names)

    # 1. Partial failure during upload staging cleans up staging/uploads/<crash_id> and returns 507
    from app.services.storage.io_ops import StorageIOMixin

    dumps_dir = api_harness.data_dir / "dumps"
    logs_dir = api_harness.data_dir / "logs"
    staging_dir = api_harness.data_dir / "staging"
    dumps_before = sorted(p.relative_to(api_harness.data_dir) for p in dumps_dir.rglob("*"))
    logs_before = sorted(p.relative_to(api_harness.data_dir) for p in logs_dir.rglob("*"))

    orig_write_stream = StorageIOMixin.atomic_write_stream
    stream_calls = 0

    def _fail_third_stream(self, *args: Any, **kwargs: Any) -> tuple[Path, int]:
        nonlocal stream_calls
        stream_calls += 1
        if stream_calls >= 3:
            raise OSError("Simulated staging disk write failure")
        return orig_write_stream(self, *args, **kwargs)

    monkeypatch.setattr(StorageIOMixin, "atomic_write_stream", _fail_third_stream)
    fail_resp = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj["api_key"]},
        data={"project_key": proj["key"], "app_version": "1.0.0"},
        files=[
            ("dump_file", ("partial.dmp", b"MDMP_PARTIAL", "application/octet-stream")),
            ("log_files", ("first.log", b"2026-11-22 10:00:00.000 INFO ok\n", "text/plain")),
            ("log_files", ("second.log", b"2026-11-22 10:00:01.000 INFO fail\n", "text/plain")),
        ],
    )
    assert fail_resp.status_code == 507
    assert sorted(p.relative_to(api_harness.data_dir) for p in dumps_dir.rglob("*")) == dumps_before
    assert sorted(p.relative_to(api_harness.data_dir) for p in logs_dir.rglob("*")) == logs_before
    assert not list(staging_dir.rglob("*"))

    # 2. Partial failure during async worker log ingestion cleans up partial files & marks FAILED
    monkeypatch.undo()
    orig_ingest = LogEngine.ingest_log
    call_count = 0

    def _fail_second_log(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise OSError("Simulated worker log disk write failure")
        return orig_ingest(self, *args, **kwargs)

    monkeypatch.setattr(LogEngine, "ingest_log", _fail_second_log)
    worker_fail_resp = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj["api_key"]},
        data={"project_key": proj["key"], "app_version": "1.0.0"},
        files=[
            ("dump_file", ("partial2.dmp", b"MDMP_PARTIAL_2", "application/octet-stream")),
            ("log_files", ("first.log", b"2026-11-22 10:00:00.000 INFO ok\n", "text/plain")),
            ("log_files", ("second.log", b"2026-11-22 10:00:01.000 INFO fail\n", "text/plain")),
        ],
    )
    assert worker_fail_resp.status_code == 202
    failed_cid = worker_fail_resp.json()["crash_id"]
    await _drain(api_harness)

    failed_detail = (await client.get(f"/api/v1/crashes/{failed_cid}")).json()
    assert failed_detail["status"] == "FAILED"
    assert sorted(p.relative_to(api_harness.data_dir) for p in dumps_dir.rglob("*")) == dumps_before
    assert sorted(p.relative_to(api_harness.data_dir) for p in logs_dir.rglob("*")) == logs_before
    assert not list(staging_dir.rglob("*"))


async def test_crash_session_id_upload_filter_related_logs_and_project_label(
    api_harness: Any,
) -> None:
    import sys
    from pathlib import Path

    client: httpx.AsyncClient = api_harness.client

    # 1. Project with custom session_id_label + validation
    proj_a_resp = await client.post(
        "/api/v1/projects",
        json={
            "key": "sess-proj-a",
            "name": "Session Project A",
            "settings_json": {"session_id_label": "  FlowID  "},
        },
    )
    assert proj_a_resp.status_code == 201
    proj_a = proj_a_resp.json()
    assert proj_a["settings_json"]["session_id_label"] == "FlowID"

    bad_label_resp = await client.patch(
        f"/api/v1/projects/{proj_a['id']}",
        json={"settings_json": {"session_id_label": "x" * 65}},
    )
    assert bad_label_resp.status_code == 422

    proj_b_resp = await client.post(
        "/api/v1/projects",
        json={"key": "sess-proj-b", "name": "Session Project B"},
    )
    assert proj_b_resp.status_code == 201
    proj_b = proj_b_resp.json()

    # 2. Invalid session_id rejected on upload
    bad_upload = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj_a["api_key"]},
        data={
            "project_key": proj_a["key"],
            "app_version": "2.0.0",
            "session_id": "bad\nline",
        },
        files={"dump_file": ("crash.dmp", b"MDMP_TEST", "application/octet-stream")},
    )
    assert bad_upload.status_code == 422

    # 3. Upload crash #1 with mid-file restart log + session_id
    log_text = "\n".join(
        [
            "2026-11-24 09:50:00.000 INFO [other-flow-1] pre-crash noise",
            "2026-11-24 10:00:01.000 INFO [flow-shared-99] session start",
            "2026-11-24 10:00:05.000 ERROR [flow-shared-99] fatal GPU hang",
            "2026-11-24 10:00:05.200 FATAL Unhandled exception dump start",
            "2026-11-24 10:00:09.000 INFO === Process restarted ===",
            "2026-11-24 10:05:00.000 ERROR [after-restart-2] unrelated error after restart",
        ]
    ).encode("utf-8")

    up1 = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj_a["api_key"]},
        data={
            "project_key": proj_a["key"],
            "app_version": "2.0.0",
            "session_id": "  flow-shared-99  ",
        },
        files=[
            ("dump_file", ("c1.dmp", b"MDMP_ONE", "application/octet-stream")),
            ("log_files", ("runtime.log", log_text, "text/plain")),
        ],
    )
    assert up1.status_code == 202
    c1_id = up1.json()["crash_id"]
    log1_id = up1.json()["log_ids"][0]

    # Upload crash #2 in SAME project with SAME session_id
    up2 = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj_a["api_key"]},
        data={
            "project_key": proj_a["key"],
            "app_version": "2.0.0",
            "session_id": "flow-shared-99",
        },
        files={"dump_file": ("c2.dmp", b"MDMP_TWO", "application/octet-stream")},
    )
    assert up2.status_code == 202
    c2_id = up2.json()["crash_id"]

    # Upload crash #3 in OTHER project with SAME session_id (must NOT leak into related crashes)
    up3 = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj_b["api_key"]},
        data={
            "project_key": proj_b["key"],
            "app_version": "2.0.0",
            "session_id": "flow-shared-99",
        },
        files={"dump_file": ("c3.dmp", b"MDMP_THREE", "application/octet-stream")},
    )
    assert up3.status_code == 202
    c3_id = up3.json()["crash_id"]

    await _drain(api_harness)

    # 4. Exact filter & fuzzy search by session_id
    exact_list = (
        await client.get(
            "/api/v1/crashes",
            params={"project_id": proj_a["id"], "session_id": "flow-shared-99"},
        )
    ).json()
    assert exact_list["total"] == 2
    assert {item["id"] for item in exact_list["items"]} == {c1_id, c2_id}
    assert all(item["session_id"] == "flow-shared-99" for item in exact_list["items"])

    fuzzy_list = (
        await client.get(
            "/api/v1/crashes",
            params={"project_id": proj_a["id"], "q": "shared-99"},
        )
    ).json()
    assert fuzzy_list["total"] == 2

    # 5. Crash detail includes session_id, mid-file session digest, and isolated related crashes
    detail = (await client.get(f"/api/v1/crashes/{c1_id}")).json()
    assert detail["session_id"] == "flow-shared-99"
    assert detail["related_session_crash_count"] == 1
    assert [r["id"] for r in detail["related_session_crashes"]] == [c2_id]
    assert c3_id not in {r["id"] for r in detail["related_session_crashes"]}
    sess_digest = detail["logs"][0]["digest_json"]["session"]
    assert sess_digest["match_count"] == 2
    assert sess_digest["first_line"] == 2
    assert sess_digest["last_line"] == 3
    assert sess_digest["trailing_lines_after_session"] == 3
    assert len(sess_digest["post_session_lines"]) == 3
    assert "Unhandled exception" in sess_digest["post_session_lines"][0]["text"]

    # 6. Log session-lines, session-scoped search, and session-scoped around-time
    sess_lines_resp = await client.get(
        f"/api/v1/logs/{log1_id}/session-lines",
        params={"start": 1, "end": 10},
    )
    assert sess_lines_resp.status_code == 200
    sl_body = sess_lines_resp.json()
    assert sl_body["session_id"] == "flow-shared-99"
    assert sl_body["total_matches"] == 2
    assert [ln["line_number"] for ln in sl_body["lines"]] == [2, 3]

    sess_search = (
        await client.get(
            f"/api/v1/logs/{log1_id}/search",
            params={"pattern": "ERROR", "only_session": "true"},
        )
    ).json()
    assert sess_search["session_id"] == "flow-shared-99"
    assert [m["line_number"] for m in sess_search["matches"]] == [3]

    sess_around = (
        await client.get(
            f"/api/v1/logs/{log1_id}/around-time",
            params={
                "timestamp": "2026-11-24T10:00:05Z",
                "window_sec": 10,
                "only_session": "true",
            },
        )
    ).json()
    assert sess_around["session_id"] == "flow-shared-99"
    assert [ln["line_number"] for ln in sess_around["lines"]] == [2, 3]

    # 7. CLI upload-crash parser accepts --session-id
    cli_dir = str(Path(__file__).resolve().parents[2] / "cli")
    if cli_dir not in sys.path:
        sys.path.insert(0, cli_dir)
    import crashkiller_cli

    parsed_cli = crashkiller_cli.build_parser().parse_args(
        [
            "upload-crash",
            "--server",
            "http://localhost:8080",
            "--project",
            "demo-game",
            "--api-key",
            "ck_key",
            "--app-version",
            "2.0.0",
            "--session-id",
            "flow-cli-01",
            "--dump",
            "c.dmp",
        ]
    )
    assert parsed_cli.session_id == "flow-cli-01"


async def test_issue_status_transitions_batch_and_history(api_harness: Any) -> None:
    client: httpx.AsyncClient = api_harness.client
    proj = (
        await client.post("/api/v1/projects", json={"key": "wf-proj", "name": "Workflow Proj"})
    ).json()

    # 1. Create issue -> initial manual event recorded
    created = (
        await client.post(
            "/api/v1/issues",
            json={"project_id": proj["id"], "title": "DXGI capture crash", "status": "NEW"},
        )
    ).json()
    issue_id = created["id"]

    detail = (await client.get(f"/api/v1/issues/{issue_id}")).json()
    assert len(detail["status_events"]) == 1
    assert detail["status_events"][0]["from_status"] is None
    assert detail["status_events"][0]["to_status"] == "NEW"
    assert detail["status_events"][0]["source"] == "manual"

    # 2. Same-status transition is rejected with 400
    same_resp = await client.post(
        f"/api/v1/issues/{issue_id}/transition",
        json={"to_status": "NEW"},
    )
    assert same_resp.status_code == 400

    # 3. Single transitions: NEW -> KNOWN -> FIXED -> REGRESSION -> KNOWN
    t1 = await client.post(
        f"/api/v1/issues/{issue_id}/transition",
        json={"to_status": "KNOWN"},
    )
    assert t1.status_code == 200
    assert t1.json()["issue"]["status"] == "KNOWN"
    assert t1.json()["event"]["from_status"] == "NEW"
    assert t1.json()["event"]["to_status"] == "KNOWN"
    assert t1.json()["event"]["source"] == "manual"

    t2 = await client.post(
        f"/api/v1/issues/{issue_id}/transition",
        json={
            "to_status": "FIXED",
            "fixed_in_version": "2.4.1",
            "note": "Fixed texture creation guard",
        },
    )
    assert t2.status_code == 200
    assert t2.json()["issue"]["status"] == "FIXED"
    assert t2.json()["issue"]["fixed_in_version"] == "2.4.1"
    assert t2.json()["event"]["fixed_in_version"] == "2.4.1"
    assert t2.json()["event"]["note"] == "Fixed texture creation guard"

    # FIXED -> REGRESSION preserves fixed_in_version by default
    t3 = await client.post(
        f"/api/v1/issues/{issue_id}/transition",
        json={"to_status": "REGRESSION", "note": "Seen again in 2.4.2"},
    )
    assert t3.status_code == 200
    assert t3.json()["issue"]["status"] == "REGRESSION"
    assert t3.json()["issue"]["fixed_in_version"] == "2.4.1"

    # REGRESSION -> KNOWN with clear_fixed_in_version clears fixed_in_version
    t4 = await client.post(
        f"/api/v1/issues/{issue_id}/transition",
        json={"to_status": "KNOWN", "clear_fixed_in_version": True},
    )
    assert t4.status_code == 200
    assert t4.json()["issue"]["status"] == "KNOWN"
    assert t4.json()["issue"]["fixed_in_version"] is None

    # 4. PATCH status change records form_edit; title-only PATCH does not add event
    p1 = await client.patch(f"/api/v1/issues/{issue_id}", json={"status": "WONTFIX"})
    assert p1.status_code == 200
    p2 = await client.patch(
        f"/api/v1/issues/{issue_id}",
        json={"title": "DXGI capture crash (updated)"},
    )
    assert p2.status_code == 200

    detail_after = (await client.get(f"/api/v1/issues/{issue_id}")).json()
    events = detail_after["status_events"]
    assert len(events) == 6
    assert events[0]["from_status"] == "KNOWN"
    assert events[0]["to_status"] == "WONTFIX"
    assert events[0]["source"] == "form_edit"
    assert [e["to_status"] for e in events] == [
        "WONTFIX",
        "KNOWN",
        "REGRESSION",
        "FIXED",
        "KNOWN",
        "NEW",
    ]

    # 5. Batch transition: updates non-matching, skips already-in-target, 404 on unknown ID
    issue_b = (
        await client.post(
            "/api/v1/issues",
            json={"project_id": proj["id"], "title": "Second issue", "status": "NEW"},
        )
    ).json()
    issue_c = (
        await client.post(
            "/api/v1/issues",
            json={
                "project_id": proj["id"],
                "title": "Already fixed",
                "status": "FIXED",
                "fixed_in_version": "2.0.0",
            },
        )
    ).json()

    not_found_batch = await client.post(
        "/api/v1/issues/batch-transition",
        json={"issue_ids": [issue_id, 999999], "to_status": "FIXED"},
    )
    assert not_found_batch.status_code == 404

    batch_resp = await client.post(
        "/api/v1/issues/batch-transition",
        json={
            "issue_ids": [issue_id, issue_b["id"], issue_c["id"]],
            "to_status": "FIXED",
            "fixed_in_version": "2.5.0",
            "note": "Released in 2.5.0",
        },
    )
    assert batch_resp.status_code == 200
    batch_data = batch_resp.json()
    assert batch_data["updated_count"] == 2
    assert batch_data["skipped_count"] == 1
    assert {item["id"] for item in batch_data["items"]} == {issue_id, issue_b["id"]}

    detail_b = (await client.get(f"/api/v1/issues/{issue_b['id']}")).json()
    assert detail_b["status"] == "FIXED"
    assert detail_b["fixed_in_version"] == "2.5.0"
    assert detail_b["status_events"][0]["source"] == "batch"
    assert detail_b["status_events"][0]["note"] == "Released in 2.5.0"

    # Already-FIXED issue was skipped and kept its original version
    detail_c = (await client.get(f"/api/v1/issues/{issue_c['id']}")).json()
    assert detail_c["fixed_in_version"] == "2.0.0"

    # 6. Merging an issue that has status_events succeeds cleanly
    merge_resp = await client.post(
        "/api/v1/issues/merge",
        json={"target_issue_id": issue_id, "source_issue_ids": [issue_b["id"]]},
    )
    assert merge_resp.status_code == 200
