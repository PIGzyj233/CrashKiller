from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.tier2


# ---------------------------------------------------------------------------
# AI Agent Guardrails & Triage Modes (T2-25 .. T2-32)
# ---------------------------------------------------------------------------


async def test_t2_25_ai_max_steps_exceeded_needs_review(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    await client.put("/api/v1/settings", json={"ai_max_steps": 2, "ai_triage_mode": "ALWAYS"})
    mock_llm_server.set_loop_forever("get_crash_overview", {})

    proj = await helpers.create_project(client, key="t2-max-steps")
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump()
    )
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    assert detail["status"] == "ANALYZED"
    assert detail["ai_verdict"] == "NEEDS_REVIEW"
    assert len(detail["ai_runs"]) == 1
    assert detail["ai_runs"][0]["result_json"]["verdict"] == "NEEDS_REVIEW"
    assert len(detail["ai_runs"][0]["trace_json"]) <= 3


async def test_t2_26_ai_timeout_exceeded_needs_review(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    await client.put("/api/v1/settings", json={"ai_timeout_seconds": 1, "ai_triage_mode": "ALWAYS"})
    mock_llm_server.set_delay(2.5)

    proj = await helpers.create_project(client, key="t2-timeout")
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump()
    )
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"], timeout=15.0)

    assert detail["status"] == "ANALYZED"
    assert detail["ai_verdict"] == "NEEDS_REVIEW"
    assert len(detail["ai_runs"]) >= 1


async def test_t2_27_ai_missing_api_key_needs_review(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    await client.put("/api/v1/settings", json={"llm_api_key": "", "ai_triage_mode": "ALWAYS"})
    proj = await helpers.create_project(client, key="t2-nokey")
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump()
    )
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    assert detail["status"] == "ANALYZED"
    assert detail["ai_verdict"] == "NEEDS_REVIEW"


async def test_t2_28_ai_tool_output_max_chars_truncation(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    await client.put(
        "/api/v1/settings", json={"ai_tool_output_max_chars": 500, "ai_triage_mode": "ALWAYS"}
    )
    proj = await helpers.create_project(client, key="t2-trunc-tool")
    verbose_log = "".join(
        f"2026-11-22 10:00:00.000 INFO verbose_line_{i}_" + ("A" * 120) + "\n" for i in range(40)
    ).encode("utf-8")

    expected_log_id: list[str] = []

    def _handler(req_json: dict[str, Any], headers: dict[str, str], idx: int) -> dict[str, Any]:
        if idx == 0:
            return mock_llm_server._build_completion_payload(
                model=req_json["model"],
                tool_calls=mock_llm_server._make_tool_call_spec([("get_crash_overview", {})])[
                    "tool_calls"
                ],
                finish_reason="tool_calls",
            )
        if idx == 1:
            tool_msgs = [m["content"] for m in req_json["messages"] if m.get("role") == "tool"]
            lid = helpers.extract_log_id(tool_msgs[0]) or (
                expected_log_id[0] if expected_log_id else ""
            )
            return mock_llm_server._build_completion_payload(
                model=req_json["model"],
                tool_calls=mock_llm_server._make_tool_call_spec(
                    [("read_log_lines", {"log_id": lid, "start_line": 1, "end_line": 40})]
                )["tool_calls"],
                finish_reason="tool_calls",
            )
        verdict = mock_llm_server._make_verdict_spec(
            verdict="NEW_ISSUE", suggested_title="Truncated Tool"
        )
        return mock_llm_server._build_completion_payload(
            model=req_json["model"], content=verdict["content"], finish_reason="stop"
        )

    mock_llm_server.set_handler(_handler)
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("v.log", verbose_log)],
    )
    expected_log_id.append(up.json()["log_ids"][0])
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    assert detail["status"] == "ANALYZED"

    # Verify the tool message sent back to LLM on turn 3 was real log content truncated <= 600 chars
    third_req_msgs = mock_llm_server.requests[2]["body"]["messages"]
    tool_msgs = [m for m in third_req_msgs if m.get("role") == "tool"]
    tool_content = tool_msgs[-1]["content"]
    assert "verbose_line_" in tool_content, f"Expected real log content, got: {tool_content!r}"
    assert "Log not found" not in tool_content
    assert 250 <= len(tool_content) <= 600


async def test_t2_29_ai_triage_mode_manual_skips_ai(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    await client.put("/api/v1/settings", json={"ai_triage_mode": "MANUAL"})
    proj = await helpers.create_project(client, key="t2-manual")
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump()
    )
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    assert detail["status"] == "ANALYZED"
    assert detail["ai_runs"] == []
    assert detail["ai_verdict"] is None
    assert len(mock_llm_server.requests) == 0


async def test_t2_30_smart_triage_unambiguous_known_cluster_skips_ai(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-smart-known")
    dmp = fake_toolchain.make_dump()
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Unambiguous Bug")
    c1 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)).json()["crash_id"],
    )
    await client.patch(f"/api/v1/issues/{c1['issue']['id']}", json={"is_ambiguous_stack": False})
    mock_llm_server.reset()

    c2 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)).json()["crash_id"],
    )
    assert c2["status"] == "ANALYZED"
    assert c2["issue"]["id"] == c1["issue"]["id"]
    assert c2["ai_runs"] == []
    assert len(mock_llm_server.requests) == 0


async def test_t2_31_smart_triage_ambiguous_stack_triggers_ai(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-smart-ambig")
    dmp = fake_toolchain.make_dump()
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Ambiguous Assert")
    c1 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)).json()["crash_id"],
    )
    await client.patch(f"/api/v1/issues/{c1['issue']['id']}", json={"is_ambiguous_stack": True})
    mock_llm_server.reset()

    mock_llm_server.queue_verdict(verdict="KNOWN_ISSUE", matched_issue_id=c1["issue"]["id"])
    c2 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)).json()["crash_id"],
    )
    assert c2["status"] == "ANALYZED"
    assert len(c2["ai_runs"]) == 1
    assert len(mock_llm_server.requests) >= 1


async def test_t2_32_regression_detection_after_fixed_version(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-regression")
    dmp = fake_toolchain.make_dump()
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Shader Crash")
    c1 = await helpers.wait_for_crash_status(
        client,
        (
            await helpers.upload_crash(
                client, proj["key"], proj["api_key"], dmp, app_version="1.1.0"
            )
        ).json()["crash_id"],
    )
    issue_id = c1["issue"]["id"]
    await client.patch(
        f"/api/v1/issues/{issue_id}",
        json={"status": "FIXED", "fixed_in_version": "1.2.0", "is_ambiguous_stack": True},
    )

    mock_llm_server.queue_verdict(verdict="REGRESSION", matched_issue_id=issue_id)
    c2 = await helpers.wait_for_crash_status(
        client,
        (
            await helpers.upload_crash(
                client, proj["key"], proj["api_key"], dmp, app_version="1.2.5"
            )
        ).json()["crash_id"],
    )
    assert c2["ai_verdict"] == "REGRESSION"


# ---------------------------------------------------------------------------
# Storage, Safety Floor, Pinning & Post-GC Persistence (T2-33 .. T2-36)
# ---------------------------------------------------------------------------


async def test_t2_33_host_safety_floor_breached(
    client: Any,
    fake_toolchain: Any,
    fake_disk_usage: Any,
    helpers: Any,
) -> None:
    fake_disk_usage.set_free_gb(40.0)
    await client.put(
        "/api/v1/settings", json={"archive_raw_symbols": True, "host_safety_floor_gb": 50.0}
    )

    proj = await helpers.create_project(client, key="t2-floor")
    did = "500011112222333344445555666677771"
    pdb = fake_toolchain.make_pdb(filename="Floor.pdb", debug_id=did)
    sym_resp = await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb, "Floor.pdb")
    assert sym_resp.status_code == 201
    assert sym_resp.json()["raw_size"] == 0
    assert (await client.get(f"/symsrv/Floor.pdb/{did}/Floor.pdb")).status_code == 404

    dash = (await client.get("/api/v1/dashboard")).json()
    assert dash["storage"]["safety_floor_breached"] is True

    gc_resp = await client.post("/api/v1/settings/gc", json={"force_emergency": False})
    assert gc_resp.status_code == 200
    assert gc_resp.json()["emergency_triggered"] is True


async def test_t2_34_is_pinned_crash_protection(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-pin-crash")
    mock_llm_server.queue_verdict()
    c_pinned = (
        await helpers.upload_crash(
            client,
            proj["key"],
            proj["api_key"],
            fake_toolchain.make_dump(extra_bytes=b"PINNED"),
            log_files=[("pin.log", helpers.make_mixed_encoding_log(num_lines=20))],
        )
    ).json()["crash_id"]
    await helpers.wait_for_crash_status(client, c_pinned)
    await client.patch(f"/api/v1/crashes/{c_pinned}/pin", json={"is_pinned": True})

    mock_llm_server.queue_verdict()
    c_unpinned = (
        await helpers.upload_crash(
            client,
            proj["key"],
            proj["api_key"],
            fake_toolchain.make_dump(extra_bytes=b"UNPINNED"),
            log_files=[("unpin.log", helpers.make_mixed_encoding_log(num_lines=20))],
        )
    ).json()["crash_id"]
    await helpers.wait_for_crash_status(client, c_unpinned)

    gc = (await client.post("/api/v1/settings/gc", json={"force_emergency": True})).json()
    assert gc["skipped_pinned_count"] >= 1
    assert (await client.get(f"/api/v1/crashes/{c_pinned}/artifacts/dump")).status_code == 200
    assert (await client.get(f"/api/v1/crashes/{c_unpinned}/artifacts/dump")).status_code in (
        404,
        410,
    )


async def test_t2_35_is_pinned_issue_protection(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-pin-issue")
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Pinned Issue")
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(extra_bytes=b"ISSUE_PINNED"),
        log_files=[("ip.log", helpers.make_mixed_encoding_log(num_lines=15))],
    )
    crash_id = up.json()["crash_id"]
    detail = await helpers.wait_for_crash_status(client, crash_id)
    issue_id = detail["issue"]["id"]

    await client.patch(f"/api/v1/issues/{issue_id}", json={"is_pinned": True})

    gc = (await client.post("/api/v1/settings/gc", json={"force_emergency": True})).json()
    assert gc["skipped_pinned_count"] >= 1
    assert (await client.get(f"/api/v1/crashes/{crash_id}/artifacts/dump")).status_code == 200


async def test_t2_36_post_gc_artifact_and_db_persistence(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-post-gc")
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Persisted Report")
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("gc.log", helpers.make_mixed_encoding_log(num_lines=20))],
    )
    crash_id = up.json()["crash_id"]
    log_id = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, crash_id)

    await client.post("/api/v1/settings/gc", json={"force_emergency": True})

    assert (await client.get(f"/api/v1/crashes/{crash_id}/artifacts/dump")).status_code in (
        404,
        410,
    )
    assert (
        await client.get(f"/api/v1/logs/{log_id}/lines", params={"start": 1, "end": 5})
    ).status_code in (404, 410)
    assert (
        await client.post(f"/api/v1/crashes/{crash_id}/reprocess", json={"mode": "full"})
    ).status_code == 409

    detail_resp = await client.get(f"/api/v1/crashes/{crash_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["parsed_stacks_json"] is not None
    assert len(detail["ai_runs"]) >= 1
