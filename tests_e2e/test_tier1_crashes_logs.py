from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.tier1


# ---------------------------------------------------------------------------
# Area 4: Crashes & SSE (T1-17 .. T1-24)
# ---------------------------------------------------------------------------


async def test_t1_17_upload_crash_dump_and_log(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="crash-up")
    dmp = fake_toolchain.make_dump()
    log_bytes = helpers.make_mixed_encoding_log(num_lines=40)
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Upload Test")

    resp = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        dump_bytes=dmp,
        log_files=[("game.log", log_bytes)],
        app_version="1.2.0",
        build_id="b42",
        os_version="Windows 11",
        crash_time="2026-11-22T10:00:30Z",
    )
    assert resp.status_code == 202
    data = resp.json()
    uuid.UUID(str(data["crash_id"]))
    assert data["status"] in ("PENDING", "PARSING", "TRIAGING", "ANALYZED")
    assert len(data["log_ids"]) == 1
    await helpers.wait_for_crash_status(client, data["crash_id"])


async def test_t1_18_list_and_filter_crashes(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="crash-list")
    dmp = fake_toolchain.make_dump()
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="List Crash")
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp, app_version="1.2.0")
    crash_id = up.json()["crash_id"]
    await helpers.wait_for_crash_status(client, crash_id)

    resp = await client.get(
        "/api/v1/crashes",
        params={
            "project_id": proj["id"],
            "status": "ANALYZED",
            "app_version": "1.2.0",
            "limit": 20,
            "offset": 0,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert any(str(c["id"]) == crash_id for c in body["items"])


async def test_t1_19_get_crash_detail(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="crash-detail")
    pdb = fake_toolchain.make_pdb()
    pe = fake_toolchain.make_pe()
    await helpers.upload_symbol(
        client, proj["key"], proj["api_key"], pdb, "MyApp.pdb", pe, "MyApp.exe"
    )

    dmp = fake_toolchain.make_dump(
        crash_reason="EXCEPTION_ACCESS_VIOLATION_READ", crash_address="0x0000000000000010"
    )
    log_bytes = helpers.make_mixed_encoding_log(num_lines=25)
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Detail Crash")
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], dmp, log_files=[("app.log", log_bytes)]
    )
    crash_id = up.json()["crash_id"]
    detail = await helpers.wait_for_crash_status(client, crash_id)

    assert detail["crash_reason"] == "EXCEPTION_ACCESS_VIOLATION_READ"
    assert detail["crash_address"] == "0x0000000000000010"
    assert len(detail["parsed_stacks_json"]["threads"]) >= 1
    assert len(detail["logs"]) == 1
    assert detail["cluster"] is not None
    assert len(detail["ai_runs"]) >= 1


async def test_t1_20_sse_realtime_status_stream(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="crash-sse")
    dmp = fake_toolchain.make_dump()
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="SSE Crash")
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)
    assert up.status_code == 202
    crash_id = up.json()["crash_id"]

    events = await helpers.stream_crash_sse(client, crash_id)
    assert len(events) >= 1
    statuses = [ev[1]["status"] for ev in events]
    assert "ANALYZED" in statuses
    last = events[-1][1]
    assert str(last["crash_id"]) == crash_id
    assert last["status"] == "ANALYZED"
    assert last["ai_verdict"] == "NEW_ISSUE"


async def test_t1_21_reprocess_crash_triage_and_full(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    await client.put("/api/v1/settings", json={"ai_triage_mode": "ALWAYS"})
    proj = await helpers.create_project(client, key="crash-reproc")
    dmp = fake_toolchain.make_dump()
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Initial Run")
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)
    crash_id = up.json()["crash_id"]
    first = await helpers.wait_for_crash_status(client, crash_id)
    initial_runs = len(first["ai_runs"])
    assert initial_runs >= 1

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Reprocess Triage Run")
    r1 = await client.post(f"/api/v1/crashes/{crash_id}/reprocess", json={"mode": "triage"})
    assert r1.status_code == 202
    cur = first
    for _ in range(300):
        cur = (await client.get(f"/api/v1/crashes/{crash_id}")).json()
        if len(cur["ai_runs"]) > initial_runs and cur["status"] == "ANALYZED":
            break
        await asyncio.sleep(0.05)
    assert cur["status"] == "ANALYZED"
    assert len(cur["ai_runs"]) == initial_runs + 1

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Reprocess Full Run")
    r2 = await client.post(f"/api/v1/crashes/{crash_id}/reprocess", json={"mode": "full"})
    assert r2.status_code == 202
    cur2 = cur
    for _ in range(300):
        cur2 = (await client.get(f"/api/v1/crashes/{crash_id}")).json()
        if len(cur2["ai_runs"]) > initial_runs + 1 and cur2["status"] == "ANALYZED":
            break
        await asyncio.sleep(0.05)
    assert cur2["status"] == "ANALYZED"
    assert len(cur2["ai_runs"]) == initial_runs + 2


async def test_t1_22_pin_and_unpin_crash(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="crash-pin")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump()
    )
    crash_id = up.json()["crash_id"]
    await helpers.wait_for_crash_status(client, crash_id)

    p1 = await client.patch(f"/api/v1/crashes/{crash_id}/pin", json={"is_pinned": True})
    assert p1.status_code == 200
    assert p1.json()["is_pinned"] is True
    assert (await client.get(f"/api/v1/crashes/{crash_id}")).json()["is_pinned"] is True

    p2 = await client.patch(f"/api/v1/crashes/{crash_id}/pin", json={"is_pinned": False})
    assert p2.status_code == 200
    assert p2.json()["is_pinned"] is False
    assert (await client.get(f"/api/v1/crashes/{crash_id}")).json()["is_pinned"] is False


async def test_t1_23_download_dump_artifact(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="art-dump")
    dmp = fake_toolchain.make_dump(extra_bytes=b"EXACT_DMP_BINARY_TRAILER_12345")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)
    crash_id = up.json()["crash_id"]
    await helpers.wait_for_crash_status(client, crash_id)

    art = await client.get(f"/api/v1/crashes/{crash_id}/artifacts/dump")
    assert art.status_code == 200
    assert "attachment" in art.headers.get("content-disposition", "").lower()
    assert art.content == dmp


async def test_t1_24_download_stacks_json_artifact(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="art-stacks")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump()
    )
    crash_id = up.json()["crash_id"]
    detail = await helpers.wait_for_crash_status(client, crash_id)

    art = await client.get(f"/api/v1/crashes/{crash_id}/artifacts/stacks_json")
    assert art.status_code == 200
    assert "attachment" in art.headers.get("content-disposition", "").lower()
    downloaded = art.json()
    assert downloaded == detail["parsed_stacks_json"]


# ---------------------------------------------------------------------------
# Area 5: Logs (T1-25 .. T1-29)
# ---------------------------------------------------------------------------


async def test_t1_25_log_summary_and_digest(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="log-sum")
    log_bytes = helpers.make_mixed_encoding_log(num_lines=200)
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("client.log", log_bytes)],
    )
    log_id = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    resp = await client.get(f"/api/v1/logs/{log_id}/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_lines"] == 200
    assert body["raw_size"] == len(log_bytes)
    assert body["compressed_size"] < body["raw_size"]
    assert "digest" in body and body["digest"]["total_lines"] == 200
    assert len(body["digest"]["recent_errors"]) >= 1
    assert len(body["digest"]["tail_lines"]) >= 1


async def test_t1_26_log_read_line_range(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="log-lines")
    log_bytes = helpers.make_mixed_encoding_log(num_lines=60)
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("l.log", log_bytes)],
    )
    log_id = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    resp = await client.get(f"/api/v1/logs/{log_id}/lines", params={"start": 10, "end": 25})
    assert resp.status_code == 200
    body = resp.json()
    assert body["start_line"] == 10
    assert body["end_line"] == 25
    assert body["total_lines"] == 60
    assert len(body["lines"]) == 16
    assert [ln["line_number"] for ln in body["lines"]] == list(range(10, 26))


async def test_t1_27_log_read_around_time(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="log-time")
    log_bytes = helpers.make_mixed_encoding_log(
        num_lines=120,
        base_time="2026-11-22T10:00:00",
        error_lines={60: "FATAL Target crash marker at 10:01:00"},
    )
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("t.log", log_bytes)],
    )
    log_id = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    resp = await client.get(
        f"/api/v1/logs/{log_id}/around-time",
        params={"timestamp": "2026-11-22T10:01:00", "window_sec": 5, "max_lines": 100},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_sec"] == 5
    texts = [ln["text"] for ln in body["lines"]]
    assert any("Target crash marker" in t for t in texts)


async def test_t1_28_log_literal_substring_search(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="log-lit")
    log_bytes = helpers.make_mixed_encoding_log(num_lines=90)
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("s.log", log_bytes)],
    )
    log_id = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    resp = await client.get(
        f"/api/v1/logs/{log_id}/search",
        params={
            "pattern": "FATAL",
            "is_regex": "false",
            "max_matches": 50,
            "case_sensitive": "true",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_matches"] >= 1
    assert body["truncated"] is False
    assert all("FATAL" in m["text"] for m in body["matches"])


async def test_t1_29_log_regex_search(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="log-regex")
    log_bytes = helpers.make_mixed_encoding_log(num_lines=90)
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("r.log", log_bytes)],
    )
    log_id = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    resp = await client.get(
        f"/api/v1/logs/{log_id}/search",
        params={
            "pattern": r"TextureCache.*null",
            "is_regex": "true",
            "max_matches": 50,
            "case_sensitive": "false",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_matches"] >= 1
    first_match = body["matches"][0]
    ln_resp = await client.get(
        f"/api/v1/logs/{log_id}/lines",
        params={"start": first_match["line_number"], "end": first_match["line_number"]},
    )
    assert ln_resp.status_code == 200
    assert ln_resp.json()["lines"][0]["text"] == first_match["text"]
