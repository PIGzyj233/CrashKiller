from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytestmark = pytest.mark.tier3


async def test_t3_09_pin_and_low_retention_manual_gc(
    client: Any,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-09-pin-ttl")
    crashes = []
    for idx in range(3):
        mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title=f"GC Issue {idx}")
        d = fake_toolchain.make_dump(
            threads=[
                {"thread_name": "M", "frames": [{"module": "App.exe", "function": f"GcFn{idx}()"}]}
            ]
        )
        up = await helpers.upload_crash(
            client,
            proj["key"],
            proj["api_key"],
            d,
            log_files=[(f"gc_{idx}.log", helpers.make_mixed_encoding_log(15))],
        )
        crashes.append(await helpers.wait_for_crash_status(client, up.json()["crash_id"]))

    c1, c2, c3 = crashes
    await client.patch(f"/api/v1/crashes/{c1['id']}/pin", json={"is_pinned": True})
    await client.patch(f"/api/v1/issues/{c2['issue']['id']}", json={"is_pinned": True})

    # Trigger non-emergency quota-based LRU eviction (host free space remains 200GB > 50GB floor)
    set_resp = await client.put(
        "/api/v1/settings",
        json={"dumps_quota_gb": 1e-9, "logs_quota_gb": 1e-9, "gc_threshold_percent": 1.0},
    )
    assert set_resp.status_code == 200

    gc = (await client.post("/api/v1/settings/gc", json={"force_emergency": False})).json()
    assert gc["emergency_triggered"] is False
    assert gc["deleted_files_count"] > 0
    assert gc["skipped_pinned_count"] >= 2

    assert (await client.get(f"/api/v1/crashes/{c1['id']}/artifacts/dump")).status_code == 200
    assert (await client.get(f"/api/v1/crashes/{c2['id']}/artifacts/dump")).status_code == 200
    assert (await client.get(f"/api/v1/crashes/{c3['id']}/artifacts/dump")).status_code in (
        404,
        410,
    )
    assert (
        await client.get(f"/api/v1/logs/{c3['logs'][0]['id']}/lines", params={"start": 1, "end": 5})
    ).status_code in (404, 410)
    c3_db = await client.get(f"/api/v1/crashes/{c3['id']}")
    assert c3_db.status_code == 200
    assert c3_db.json()["parsed_stacks_json"] is not None
    assert len(c3_db.json()["ai_runs"]) >= 1


async def test_t3_10_dynamic_settings_ai_mode_model_steps_and_noise(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-10-dynset")
    await client.put(
        "/api/v1/settings",
        json={
            "ai_triage_mode": "MANUAL",
            "default_noise_modules": ["ntdll.dll", "wrapper.dll"],
        },
    )
    dmp = fake_toolchain.make_dump(
        modules=[
            {
                "filename": "wrapper.dll",
                "debug_file": "wrapper.pdb",
                "debug_id": "W1",
                "always_loaded": True,
            },
            {
                "filename": "App.exe",
                "debug_file": "App.pdb",
                "debug_id": "A1",
                "always_loaded": True,
            },
        ],
        threads=[
            {
                "thread_name": "M",
                "frames": [
                    {"module": "wrapper.dll", "function": "Wrapper::Trap()"},
                    {"module": "App.exe", "function": "RealCrash::Boom()"},
                ],
            }
        ],
    )
    c1 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)).json()["crash_id"],
    )
    assert c1["ai_runs"] == []
    sig_frames_str = str(c1["cluster"]["signature_frames_json"])
    assert "wrapper.dll" not in sig_frames_str
    assert "RealCrash::Boom" in sig_frames_str

    await client.put(
        "/api/v1/settings",
        json={"ai_triage_mode": "ALWAYS", "llm_model": "custom-gpt", "ai_max_steps": 2},
    )
    mock_llm_server.set_loop_forever("get_crash_overview", {})
    c2 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)).json()["crash_id"],
    )
    assert len(c2["ai_runs"]) == 1
    assert c2["ai_runs"][0]["model_name"] == "custom-gpt"
    assert c2["ai_verdict"] == "NEEDS_REVIEW"
    assert all(r["body"]["model"] == "custom-gpt" for r in mock_llm_server.requests)


async def test_t3_14_project_settings_overrides_vs_global(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    await client.put("/api/v1/settings", json={"ai_triage_mode": "SMART_TRIAGE"})
    proj_a = await helpers.create_project(
        client,
        key="t3-14-a",
        settings_json={"noise_modules": ["custom_wrapper.dll"], "ai_triage_mode": "MANUAL"},
    )
    proj_b = await helpers.create_project(client, key="t3-14-b")

    dmp = fake_toolchain.make_dump(
        modules=[
            {
                "filename": "custom_wrapper.dll",
                "debug_file": "cw.pdb",
                "debug_id": "CW1",
                "always_loaded": True,
            },
            {
                "filename": "App.exe",
                "debug_file": "App.pdb",
                "debug_id": "AP1",
                "always_loaded": True,
            },
        ],
        threads=[
            {
                "thread_name": "M",
                "frames": [
                    {"module": "custom_wrapper.dll", "function": "Wrapper::Enter()"},
                    {"module": "App.exe", "function": "Core::Run()"},
                ],
            }
        ],
    )

    c_a = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj_a["key"], proj_a["api_key"], dmp)).json()[
            "crash_id"
        ],
    )
    assert c_a["ai_runs"] == []
    assert "custom_wrapper.dll" not in str(c_a["cluster"]["signature_frames_json"])

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Proj B Crash")
    c_b = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj_b["key"], proj_b["api_key"], dmp)).json()[
            "crash_id"
        ],
    )
    assert len(c_b["ai_runs"]) == 1
    assert "custom_wrapper.dll" in str(c_b["cluster"]["signature_frames_json"])


async def test_t3_15_cross_project_isolation(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    p1 = await helpers.create_project(client, key="t3-15-p1")
    p2 = await helpers.create_project(client, key="t3-15-p2")

    dmp = fake_toolchain.make_dump(
        modules=[
            {
                "filename": "Mod.dll",
                "debug_file": "Mod.pdb",
                "debug_id": "ISO111",
                "always_loaded": False,
            }
        ],
        threads=[{"thread_name": "M", "frames": [{"module": "Mod.dll", "function": "F()"}]}],
    )
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="P1 Issue")
    c1 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, p1["key"], p1["api_key"], dmp)).json()["crash_id"],
    )
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="P2 Issue")
    c2 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, p2["key"], p2["api_key"], dmp)).json()["crash_id"],
    )

    assert c1["cluster"]["signature_hash"] != c2["cluster"]["signature_hash"]
    assert (await helpers.upload_crash(client, p2["key"], p1["api_key"], dmp)).status_code == 403
    assert (
        await client.post(
            "/api/v1/issues/merge",
            json={"target_issue_id": c1["issue"]["id"], "source_issue_ids": [c2["issue"]["id"]]},
        )
    ).status_code == 400
    assert (
        await client.post(
            f"/api/v1/crashes/{c1['id']}/verdict",
            json={"action": "assign_issue", "issue_id": c2["issue"]["id"]},
        )
    ).status_code in (400, 404)

    miss_p1 = (await client.get("/api/v1/symbols/missing", params={"project_id": p1["id"]})).json()
    assert all(c2["id"] not in [str(x) for x in m["affected_crash_ids"]] for m in miss_p1)


async def test_t3_16_safety_floor_blocks_raw_archive_and_triggers_emergency_gc(
    client: Any,
    fake_toolchain: Any,
    fake_disk_usage: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-16-floor")
    await client.put(
        "/api/v1/settings", json={"archive_raw_symbols": True, "host_safety_floor_gb": 50.0}
    )

    mock_llm_server.queue_verdict()
    c_pin = (
        await helpers.upload_crash(
            client, proj["key"], proj["api_key"], fake_toolchain.make_dump(extra_bytes=b"PIN")
        )
    ).json()["crash_id"]
    await helpers.wait_for_crash_status(client, c_pin)
    await client.patch(f"/api/v1/crashes/{c_pin}/pin", json={"is_pinned": True})

    mock_llm_server.queue_verdict()
    c_unpin = (
        await helpers.upload_crash(
            client, proj["key"], proj["api_key"], fake_toolchain.make_dump(extra_bytes=b"UNPIN")
        )
    ).json()["crash_id"]
    await helpers.wait_for_crash_status(client, c_unpin)

    fake_disk_usage.set_free_gb(40.0)
    did = "316011112222333344445555666677771"
    sym = await helpers.upload_symbol(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_pdb(filename="S16.pdb", debug_id=did),
        "S16.pdb",
    )
    assert sym.status_code == 201
    assert sym.json()["raw_size"] == 0
    assert (await client.get(f"/symsrv/S16.pdb/{did}/S16.pdb")).status_code == 404

    dash = (await client.get("/api/v1/dashboard")).json()
    assert dash["storage"]["safety_floor_breached"] is True

    gc = (await client.post("/api/v1/settings/gc", json={"force_emergency": True})).json()
    assert gc["emergency_triggered"] is True
    assert (await client.get(f"/api/v1/crashes/{c_pin}/artifacts/dump")).status_code == 200
    assert (await client.get(f"/api/v1/crashes/{c_unpin}/artifacts/dump")).status_code in (404, 410)


async def test_t3_17_post_gc_reprocess_modes_and_degradation(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-17-postgc")
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Initial")
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("g.log", helpers.make_mixed_encoding_log(15))],
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

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Triage Re-run")
    tr = await client.post(f"/api/v1/crashes/{crash_id}/reprocess", json={"mode": "triage"})
    assert tr.status_code == 202
    cur: dict[str, Any] = {}
    for _ in range(300):
        cur = (await client.get(f"/api/v1/crashes/{crash_id}")).json()
        if len(cur["ai_runs"]) >= 2 and cur["status"] == "ANALYZED":
            break
        await asyncio.sleep(0.05)
    assert cur["status"] == "ANALYZED"
    assert len(cur["ai_runs"]) >= 2
    assert cur["ai_runs"][-1]["status"] == "COMPLETED"


async def test_t3_18_ai_failure_settings_recovery_and_reprocess_sse(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-18-recover")
    mock_llm_server.set_error_status(500, "LLM down")
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump()
    )
    crash_id = up.json()["crash_id"]
    failed_triage = await helpers.wait_for_crash_status(client, crash_id)
    assert failed_triage["ai_verdict"] == "NEEDS_REVIEW"
    assert failed_triage["parsed_stacks_json"] is not None

    mock_llm_server.reset()
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Recovered Issue")
    reproc = await client.post(f"/api/v1/crashes/{crash_id}/reprocess", json={"mode": "triage"})
    assert reproc.status_code == 202

    events = await helpers.stream_crash_sse(client, crash_id)
    assert events[-1][1]["status"] == "ANALYZED"
    detail = (await client.get(f"/api/v1/crashes/{crash_id}")).json()
    assert len(detail["ai_runs"]) == 2
    assert detail["ai_runs"][-1]["status"] == "COMPLETED"
    assert detail["ai_verdict"] == "NEW_ISSUE"


async def test_t3_21_dashboard_aggregations_accuracy(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-21-dash-acc")
    # 1 corrupt -> FAILED
    u_fail = await helpers.upload_crash(client, proj["key"], proj["api_key"], b"CORRUPT")
    await helpers.wait_for_crash_status(
        client, u_fail.json()["crash_id"], target_statuses=("FAILED",)
    )

    # 1 missing symbol -> NEW_ISSUE
    d_miss = fake_toolchain.make_dump(
        modules=[
            {
                "filename": "M21.dll",
                "debug_file": "M21.pdb",
                "debug_id": "M21ID",
                "always_loaded": False,
            }
        ],
        threads=[{"thread_name": "M", "frames": [{"module": "M21.dll", "function": "X()"}]}],
    )
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Missing M21")
    u_miss = await helpers.upload_crash(client, proj["key"], proj["api_key"], d_miss)
    await helpers.wait_for_crash_status(client, u_miss.json()["crash_id"])

    # 1 timeout -> NEEDS_REVIEW
    await client.put("/api/v1/settings", json={"ai_timeout_seconds": 1})
    mock_llm_server.set_delay(2.5)
    d_nr = fake_toolchain.make_dump(
        threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": "Slow()"}]}]
    )
    u_nr = await helpers.upload_crash(client, proj["key"], proj["api_key"], d_nr)
    await helpers.wait_for_crash_status(client, u_nr.json()["crash_id"])

    dash = (
        await client.get("/api/v1/dashboard", params={"project_id": proj["id"], "days": 14})
    ).json()
    assert dash["kpis"]["total_crashes"] == 3
    assert dash["kpis"]["analyzed_crashes"] == 2
    assert dash["kpis"]["missing_symbols_count"] >= 1
    assert dash["verdict_distribution"]
    verdict_str = str(dash["verdict_distribution"])
    assert "NEW_ISSUE" in verdict_str and "NEEDS_REVIEW" in verdict_str
    assert dash["top_missing_symbols"]
    assert "M21" in str(dash["top_missing_symbols"])
    assert len(dash["crash_trend"]) >= 1
    assert (await client.get("/api/v1/dashboard", params={"days": 0})).status_code == 422


async def test_t3_22_ai_tool_truncation_and_max_steps_guardrail(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    await client.put(
        "/api/v1/settings",
        json={"ai_tool_output_max_chars": 400, "ai_max_steps": 3, "ai_triage_mode": "ALWAYS"},
    )
    proj = await helpers.create_project(client, key="t3-22-guard")
    big_log = "".join(
        f"2026-11-22 10:00:00.000 INFO line_{i}_" + ("B" * 100) + "\n" for i in range(40)
    ).encode("utf-8")

    expected_log_id: list[str] = []

    def _loop_handler(
        req_json: dict[str, Any], headers: dict[str, str], idx: int
    ) -> dict[str, Any]:
        if idx == 0:
            return mock_llm_server._build_completion_payload(
                model=req_json["model"],
                tool_calls=mock_llm_server._make_tool_call_spec([("get_crash_overview", {})])[
                    "tool_calls"
                ],
                finish_reason="tool_calls",
            )
        tool_msgs = [m for m in req_json["messages"] if m.get("role") == "tool"]
        lid = helpers.extract_log_id(tool_msgs[0]["content"]) or (
            expected_log_id[0] if expected_log_id else ""
        )
        return mock_llm_server._build_completion_payload(
            model=req_json["model"],
            tool_calls=mock_llm_server._make_tool_call_spec(
                [("read_log_lines", {"log_id": lid, "start_line": 1, "end_line": 40})]
            )["tool_calls"],
            finish_reason="tool_calls",
        )

    mock_llm_server.set_handler(_loop_handler)
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("b.log", big_log)],
    )
    expected_log_id.append(up.json()["log_ids"][0])
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    assert detail["ai_verdict"] == "NEEDS_REVIEW"
    run = detail["ai_runs"][0]
    assert len(run["trace_json"]) <= 4
    third_tool_msgs = [
        m for m in mock_llm_server.requests[2]["body"]["messages"] if m.get("role") == "tool"
    ]
    tool_content = third_tool_msgs[-1]["content"]
    assert "line_" in tool_content, f"Expected real log content, got: {tool_content!r}"
    assert "Log not found" not in tool_content
    assert 250 <= len(tool_content) <= 500


async def test_t3_23_human_verdict_actions_and_list_filtering(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-23-verdicts")
    manual_issue = (
        await client.post(
            "/api/v1/issues",
            json={
                "project_id": proj["id"],
                "title": "Manual Target",
                "status": "KNOWN",
                "component": "Net",
            },
        )
    ).json()

    crashes = []
    for idx in range(3):
        mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title=f"Auto {idx}")
        d = fake_toolchain.make_dump(
            threads=[
                {"thread_name": "M", "frames": [{"module": "App.exe", "function": f"VFn{idx}()"}]}
            ]
        )
        up = await helpers.upload_crash(client, proj["key"], proj["api_key"], d)
        crashes.append(await helpers.wait_for_crash_status(client, up.json()["crash_id"]))

    await client.post(f"/api/v1/crashes/{crashes[0]['id']}/verdict", json={"action": "confirm"})
    await client.post(
        f"/api/v1/crashes/{crashes[1]['id']}/verdict",
        json={"action": "assign_issue", "issue_id": manual_issue["id"]},
    )
    await client.post(
        f"/api/v1/crashes/{crashes[2]['id']}/verdict",
        json={
            "action": "create_issue",
            "new_issue": {"title": "Created From Verdict", "component": "UI"},
        },
    )

    verified = (
        await client.get(
            "/api/v1/crashes", params={"project_id": proj["id"], "is_human_verified": "true"}
        )
    ).json()
    assert verified["total"] == 3

    net_issues = (
        await client.get(
            "/api/v1/issues",
            params={"project_id": proj["id"], "status": "KNOWN", "component": "Net"},
        )
    ).json()
    assert net_issues["total"] == 1 and net_issues["items"][0]["crash_count"] == 1

    clusters = (
        await client.get(
            "/api/v1/issues/clusters", params={"project_id": proj["id"], "unassigned_only": "false"}
        )
    ).json()
    assert clusters["total"] == 3
