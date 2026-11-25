from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import _load_backend_public

pytestmark = pytest.mark.tier5


async def test_t5_04_db_string_bounds_and_long_filename_hardening(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    """Upload metadata enforces String(N) bounds; filenames/toolchain/LLM fields truncate."""
    proj = await helpers.create_project(client, key="t5-bounds")
    pdb = fake_toolchain.make_pdb()
    dmp = fake_toolchain.make_dump(
        crash_reason="R" * 320,
        crash_address="0x" + ("F" * 180),
        system_info={"os": "Windows", "os_ver": "10.0." + ("9" * 300), "cpu": "amd64"},
    )

    # Oversized upload metadata rejected cleanly with 422
    assert (
        await helpers.upload_symbol(
            client, proj["key"], proj["api_key"], pdb, app_version="V" * 129
        )
    ).status_code == 422
    assert (
        await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp, app_version="V" * 129)
    ).status_code == 422
    assert (
        await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp, build_id="B" * 129)
    ).status_code == 422
    assert (
        await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp, os_version="O" * 256)
    ).status_code == 422

    # Long log filename + oversized toolchain/LLM fields persist safely within column bounds
    mock_llm_server.queue_verdict(
        verdict="NEW_ISSUE",
        suggested_title="T" * 700,
        suggested_component="C" * 300,
    )
    long_log_name = ("a" * 600) + ".log"
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        dmp,
        log_files=[
            (long_log_name, b"2026-11-22 10:00:00.000 ERROR bounds test\n"),
            ("..\\..\\evil\x00.log", b"2026-11-22 10:00:01.000 INFO evil name\n"),
        ],
    )
    assert up.status_code == 202
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    assert detail["status"] == "ANALYZED"
    assert len(detail["crash_reason"]) <= 255
    assert len(detail["crash_address"]) <= 128
    assert len(detail["os_version"]) <= 255
    assert len(detail["issue"]["title"]) <= 512
    assert len(detail["issue"]["component"]) <= 128

    for lg in detail["logs"]:
        assert len(lg["filename"]) <= 512
        assert ".." not in lg["filename"]
        assert "\x00" not in lg["filename"]
    assert any(
        lg["filename"].endswith(".log") and len(lg["filename"]) == 512 for lg in detail["logs"]
    )


async def test_t5_05_ai_overflow_matched_issue_id_and_non_finite_confidence(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    """Out-of-range int32 matched_issue_id & NaN confidence degrade to NEEDS_REVIEW."""
    proj = await helpers.create_project(
        client,
        key="t5-ai-int32",
        settings_json={"ai_triage_mode": "ALWAYS"},
    )
    dmp = fake_toolchain.make_dump()

    for bad_id in (10**20, 0, -5):
        mock_llm_server.queue_tool_call("get_crash_overview")
        mock_llm_server.queue_response(
            status=200,
            body=mock_llm_server._build_completion_payload(
                model="mock-gpt-4o-mini",
                content=(
                    '{"verdict": "KNOWN_ISSUE", '
                    f'"matched_issue_id": {bad_id}, '
                    '"confidence": NaN, '
                    '"suggested_title": "Overflow ID Test", '
                    '"root_cause_analysis": "Test", '
                    '"reasoning_summary": "Test", '
                    '"evidence_log_snippets": [], '
                    '"key_log_patterns": []}'
                ),
            ),
        )
        up = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)
        assert up.status_code == 202
        detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
        assert detail["status"] == "ANALYZED"
        assert detail["ai_verdict"] == "NEEDS_REVIEW"
        assert 0.0 <= detail["ai_confidence"] <= 1.0


async def test_t5_12_nanosecond_timezone_log_around_time_accuracy(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    """Nanosecond (>6 digit) +08:00 timestamps must index accurately in UTC."""
    proj = await helpers.create_project(
        client,
        key="t5-nano-tz",
        settings_json={"ai_triage_mode": "MANUAL"},
    )
    log_bytes = (
        b"2026-11-22T10:00:06.123456789+08:00 ERROR nano_plus8_event\n"
        b"2026-11-22T02:00:07.987654321Z INFO nano_utc_event\n"
    )

    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("nano.log", log_bytes)],
    )
    assert up.status_code == 202
    lid = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    hit = (
        await client.get(
            f"/api/v1/logs/{lid}/around-time",
            params={"timestamp": "2026-11-22T02:00:06Z", "window_sec": 5},
        )
    ).json()
    assert [ln["line_number"] for ln in hit["lines"]] == [1, 2]
    assert "nano_plus8_event" in hit["lines"][0]["text"]

    miss = (
        await client.get(
            f"/api/v1/logs/{lid}/around-time",
            params={"timestamp": "2026-11-22T10:00:06Z", "window_sec": 5},
        )
    ).json()
    assert miss["lines"] == []


async def test_t5_13_log_engine_tricky_regex_unicode_and_out_of_range_bounds(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    """Empty logs, out-of-range /lines bounds, scoped flags, and Unicode search work cleanly."""
    proj = await helpers.create_project(
        client,
        key="t5-regex-unicode",
        settings_json={"ai_triage_mode": "MANUAL"},
    )
    log_text = (
        "\n".join(
            [
                "2026-11-22 10:00:00.000 INFO STRAẞE path",
                "2026-11-22 10:00:01.000 INFO ſample descriptor",
                "2026-11-22 10:00:02.000 INFO Kelvin sensor",
                "2026-11-22 10:00:03.000 INFO İstanbul gateway",
                "",
                "2026-11-22 10:00:04.000 ERROR (?m) line anchor check",
            ]
        )
        + "\n"
    )

    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[
            ("empty.log", b""),
            ("unicode.log", log_text.encode("utf-8")),
        ],
    )
    assert up.status_code == 202
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    logs_by_name = {lg["filename"]: lg["id"] for lg in detail["logs"]}
    empty_id = logs_by_name["empty.log"]
    uni_id = logs_by_name["unicode.log"]

    # Empty log boundary
    empty_lines = (await client.get(f"/api/v1/logs/{empty_id}/lines")).json()
    assert empty_lines["lines"] == []
    assert empty_lines["start_line"] == 0
    assert empty_lines["end_line"] == 0
    assert (await client.get(f"/api/v1/logs/{empty_id}/search", params={"pattern": "foo"})).json()[
        "matches"
    ] == []
    assert (
        await client.get(
            f"/api/v1/logs/{empty_id}/around-time",
            params={"timestamp": "2026-11-22T10:00:00Z"},
        )
    ).json()["lines"] == []

    # Out-of-range /lines on non-empty log returns start_line=0, end_line=0
    oob = (
        await client.get(f"/api/v1/logs/{uni_id}/lines", params={"start": 500, "end": 600})
    ).json()
    assert oob["lines"] == []
    assert oob["start_line"] == 0
    assert oob["end_line"] == 0

    # Tricky Unicode & scoped flag regexes
    checks = [
        ("(?i)straße", [1]),
        ("(?i)sample", [2]),
        ("(?i)kelvin", [3]),
        ("(?-i:İstanbul)", [4]),
        ("(?-m:^2026)", [1, 2, 3, 4, 6]),
        (r"\A2026", [1, 2, 3, 4, 6]),
        ("^$", [5]),
    ]
    for pattern, expected_lines in checks:
        resp = await client.get(
            f"/api/v1/logs/{uni_id}/search",
            params={"pattern": pattern, "is_regex": "true"},
        )
        assert resp.status_code == 200, f"Pattern {pattern!r} failed: {resp.text}"
        got = [m["line_number"] for m in resp.json()["matches"]]
        assert got == expected_lines, f"Pattern {pattern!r}: expected {expected_lines}, got {got}"

    assert (
        await client.get(
            f"/api/v1/logs/{uni_id}/search",
            params={"pattern": "[unclosed", "is_regex": "true"},
        )
    ).status_code == 422


async def test_t5_14_ai_get_other_threads_null_index_and_malformed_stackwalk_json(
    isolated_env: dict[str, Any],
    mock_llm_server: Any,
    helpers: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """get_other_threads excludes null-index crashing thread; parser handles malformed JSON."""
    custom_stackwalk = tmp_path / "custom_stackwalk.py"
    custom_stackwalk.write_text(
        """#!/usr/bin/env python3
import json, sys
payload = {
    "status": "OK",
    "crash_info": {
        "type": "EXCEPTION_ACCESS_VIOLATION_READ",
        "address": "0x10",
        "crashing_thread": 0,
    },
    "system_info": {"os": "Windows NT", "os_ver": "10.0"},
    "threads": [
        {
            "thread_index": None,
            "thread_name": "CrashingSecretThread",
            "frames": [
                {
                    "frame": "not-an-int",
                    "module": "App.exe",
                    "function": "SecretCrashFrame()",
                    "registers": ["bad", "list"],
                }
            ],
        },
        {
            "thread_index": None,
            "thread_name": "WorkerThread",
            "frames": [{"frame": 0, "module": "App.exe", "function": "WorkerLoop()"}],
        },
    ],
    "modules": [],
}
sys.stdout.write(json.dumps(payload))
""",
        encoding="utf-8",
    )
    custom_stackwalk.chmod(0o755)
    monkeypatch.setenv("MINIDUMP_STACKWALK_BIN", str(custom_stackwalk))

    create_app, get_settings = _load_backend_public()
    get_settings.cache_clear()
    app = create_app()

    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                timeout=30.0,
                auth=isolated_env["admin_auth"],
            ) as ac:
                proj = await helpers.create_project(
                    ac,
                    key="t5-null-tidx",
                    settings_json={"ai_triage_mode": "ALWAYS"},
                )
                mock_llm_server.queue_tool_call("get_other_threads", {})
                mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Safe Parse")

                up = await ac.post(
                    "/api/v1/crashes/upload",
                    headers={"X-API-Key": proj["api_key"]},
                    data={"project_key": proj["key"], "app_version": "1.0.0"},
                    files={"dump_file": ("crash.dmp", b"MDMP_DUMMY", "application/octet-stream")},
                )
                assert up.status_code == 202
                detail = await helpers.wait_for_crash_status(ac, up.json()["crash_id"])
                assert detail["status"] == "ANALYZED"
                assert detail["parsed_stacks_json"]["threads"][0]["frames"][0]["frame_index"] == 0
                assert detail["parsed_stacks_json"]["threads"][0]["frames"][0]["registers"] == {}

                # Inspect tool response sent to LLM
                tool_msgs = [
                    m
                    for req in mock_llm_server.requests
                    for m in req["body"].get("messages", [])
                    if m.get("role") == "tool"
                ]
                assert len(tool_msgs) >= 1
                other_threads_payload = json.loads(tool_msgs[0]["content"])
                assert other_threads_payload["total_other_threads"] == 1
                assert len(other_threads_payload["threads"]) == 1
                assert other_threads_payload["threads"][0]["thread_name"] == "WorkerThread"
                assert "SecretCrashFrame" not in tool_msgs[0]["content"]
    finally:
        get_settings.cache_clear()


async def test_t5_15_pagination_settings_bounds_and_nan_inf_rejection(
    client: Any,
) -> None:
    """List pagination limits and PUT /settings numeric/boolean/NaN validation must enforce 422."""
    for path in (
        "/api/v1/symbols",
        "/api/v1/symbols/missing",
        "/api/v1/crashes",
        "/api/v1/issues",
        "/api/v1/issues/clusters",
    ):
        for bad_params in (
            {"limit": 0},
            {"limit": -1},
            {"limit": 501},
            {"offset": -1},
        ):
            r = await client.get(path, params=bad_params)
            assert r.status_code == 422, f"{path} {bad_params} -> {r.status_code}"

    for bad_payload in (
        {"ai_tool_output_max_chars": 99},
        {"ai_tool_output_max_chars": 200001},
        {"ai_max_steps": 0},
        {"ai_max_steps": 51},
        {"ai_max_steps": 1.5},
        {"ai_max_steps": True},
        {"ai_timeout_seconds": 3601},
        {"gc_threshold_percent": 0},
        {"gc_threshold_percent": 100.1},
        {"log_retention_days": 3651},
        {"staging_ttl_hours": 721},
    ):
        r = await client.put("/api/v1/settings", json=bad_payload)
        assert r.status_code == 422, f"{bad_payload} -> {r.status_code}"

    # Raw JSON NaN / Infinity rejection
    nan_resp = await client.put(
        "/api/v1/settings",
        headers={"Content-Type": "application/json"},
        content=b'{"gc_threshold_percent": NaN}',
    )
    assert nan_resp.status_code == 422

    # Valid boundary values accepted
    ok_resp = await client.put(
        "/api/v1/settings",
        json={
            "gc_threshold_percent": 100.0,
            "ai_tool_output_max_chars": 100,
            "total_disk_quota_gb": 0.0,
        },
    )
    assert ok_resp.status_code == 200
    assert ok_resp.json()["gc_threshold_percent"] == 100.0
    assert ok_resp.json()["ai_tool_output_max_chars"] == 100


async def test_t5_16_symsrv_traversal_malformed_multipart_sse_disconnect_and_corrupt_zst(
    client: Any,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    """Test traversal, malformed multipart, empty dashboard, SSE disconnect, and corrupt zst."""
    # Empty DB & nonexistent project_id dashboard -> 200 zeroed KPIs
    empty_dash = await client.get(
        "/api/v1/dashboard",
        params={"project_id": str(uuid.uuid4()), "days": 1},
    )
    assert empty_dash.status_code == 200
    assert empty_dash.json()["kpis"]["total_crashes"] == 0

    # /symsrv traversal & mismatched target filename -> 404
    trav_url = "/symsrv/..%2F..%2Fetc%2Fpasswd/123/..%2F..%2Fetc%2Fpasswd"
    assert (await client.get(trav_url)).status_code == 404
    assert (await client.get("/symsrv/MyApp.pdb/123/Other.pdb")).status_code == 404

    proj = await helpers.create_project(
        client,
        key="t5-sec-sse",
        settings_json={"ai_triage_mode": "ALWAYS"},
    )
    pdb = fake_toolchain.make_pdb()

    # Malformed symbol filename ".." -> 422; path-prefixed filename sanitized to basename -> 201
    bad_sym = await helpers.upload_symbol(
        client, proj["key"], proj["api_key"], pdb, pdb_filename=".."
    )
    assert bad_sym.status_code == 422
    san_sym = await helpers.upload_symbol(
        client, proj["key"], proj["api_key"], pdb, pdb_filename="../../MyApp.pdb"
    )
    assert san_sym.status_code == 201

    # Hold LLM so SSE disconnect occurs while crash is TRIAGING
    mock_llm_server.set_delay(0.6)
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="SSE Disconnect Test")
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
    )
    cid = up.json()["crash_id"]

    # Artifact path traversal -> 422/404
    bad_art = await client.get(f"/api/v1/crashes/{cid}/artifacts/../../etc/passwd")
    assert bad_art.status_code in (404, 422)

    # Connect SSE and close early after receiving first event
    async with client.stream("GET", f"/api/v1/crashes/{cid}/events") as sse:
        assert sse.status_code == 200
        async for line in sse.aiter_lines():
            if line.startswith("data:"):
                break

    mock_llm_server.set_delay(0.0)
    # Second SSE stream completes cleanly without leaked subscriptions
    events = await helpers.stream_crash_sse(client, cid, timeout=10.0)
    assert any(evt.get("status") == "ANALYZED" for _, evt in events)

    # Corrupt stored .dmp.zst on disk -> reprocess(full) marks FAILED cleanly + emits SSE FAILED
    zst_path = isolated_env["data_dir"] / "dumps" / cid / f"{cid}.dmp.zst"
    assert zst_path.is_file()
    zst_path.write_bytes(b"CORRUPT_ZST_HEADER")

    reproc = await client.post(f"/api/v1/crashes/{cid}/reprocess", json={"mode": "full"})
    assert reproc.status_code == 202
    failed_detail = await helpers.wait_for_crash_status(client, cid)
    assert failed_detail["status"] == "FAILED"

    late_events = await helpers.stream_crash_sse(client, cid, timeout=5.0)
    assert any(evt.get("status") == "FAILED" and evt.get("error") for _, evt in late_events)
