from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.tier3


async def test_t3_01_symbols_symsrv_parse_and_ai_triage(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    await client.put("/api/v1/settings", json={"archive_raw_symbols": True})
    proj = await helpers.create_project(client, key="t3-01-symsrv")

    did = "310011112222333344445555666677771"
    cid = "650031001122"
    pdb = fake_toolchain.make_pdb(
        filename="GameEngine.pdb",
        debug_id=did,
        code_file="GameEngine.exe",
        code_id=cid,
        extra_bytes=b"RAW_ENGINE_PDB",
    )
    pe = fake_toolchain.make_pe(
        filename="GameEngine.exe",
        code_id=cid,
        debug_file="GameEngine.pdb",
        debug_id=did,
        extra_bytes=b"RAW_ENGINE_PE",
    )
    sym = await helpers.upload_symbol(
        client, proj["key"], proj["api_key"], pdb, "GameEngine.pdb", pe, "GameEngine.exe"
    )
    assert sym.status_code == 201
    assert sym.json()["has_pe_unwind"] is True
    sym_id = sym.json()["id"]
    first_used = sym.json()["last_used_at"]

    dmp = fake_toolchain.make_dump(
        modules=[
            {
                "filename": "GameEngine.exe",
                "debug_file": "GameEngine.pdb",
                "debug_id": did,
                "code_file": "GameEngine.exe",
                "code_id": cid,
                "always_loaded": False,
            }
        ],
        threads=[
            {
                "thread_name": "MainThread",
                "frames": [
                    {
                        "module": "GameEngine.exe",
                        "function": "Engine::RenderFrame()",
                        "file": "src/engine.cpp",
                        "line": 210,
                    }
                ],
            }
        ],
    )
    log_bytes = helpers.make_mixed_encoding_log(num_lines=20)
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Engine::RenderFrame Crash")

    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], dmp, log_files=[("engine.log", log_bytes)]
    )
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    assert detail["status"] == "ANALYZED"
    assert detail["missing_symbols_json"] == []
    top_frame = detail["parsed_stacks_json"]["threads"][0]["frames"][0]
    assert top_frame["trust"] == "cfi"
    assert top_frame["file"] == "src/engine.cpp"
    assert top_frame["line"] == 210

    assert (await client.get("/symsrv/pingme.txt")).status_code == 200
    assert (await client.get("/symsrv/index2.txt")).status_code == 404
    assert (await client.head(f"/symsrv/GameEngine.pdb/{did}/GameEngine.pdb")).status_code == 200
    raw_pdb = await client.get(f"/symsrv/GameEngine.pdb/{did}/GameEngine.pdb")
    assert raw_pdb.status_code == 200 and raw_pdb.content == pdb
    assert (await client.get(f"/symsrv/GameEngine.pdb/NONEXISTENT/GameEngine.pdb")).status_code == 404

    syms_after = (await client.get("/api/v1/symbols", params={"project_id": proj["id"]})).json()["items"]
    matched = [s for s in syms_after if s["id"] == sym_id][0]
    assert matched["last_used_at"] >= first_used


async def test_t3_02_missing_symbols_upload_resymbolicate_and_recluster(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-02-resym")
    did = "D10011112222333344445555666677771"
    cid = "C0DED100"
    dmp = fake_toolchain.make_dump(
        modules=[
            {
                "filename": "Core.dll",
                "debug_file": "Core.pdb",
                "debug_id": did,
                "code_file": "Core.dll",
                "code_id": cid,
                "always_loaded": False,
            }
        ],
        threads=[
            {
                "thread_name": "Main",
                "frames": [
                    {"module": "Core.dll", "function": "Core::AllocateBuffer(size_t)", "file": "src/mem.cpp", "line": 88}
                ],
            }
        ],
    )

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Unresolved Core Crash")
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)
    crash_id = up.json()["crash_id"]
    before = await helpers.wait_for_crash_status(client, crash_id)
    sig_before = before["cluster"]["signature_hash"]
    assert any(m["debug_id"] == did for m in before["missing_symbols_json"])

    miss_list = (await client.get("/api/v1/symbols/missing", params={"project_id": proj["id"]})).json()
    assert any(m["debug_id"] == did and m["crash_count"] == 1 for m in miss_list)

    pdb = fake_toolchain.make_pdb(filename="Core.pdb", debug_id=did, code_file="Core.dll", code_id=cid)
    pe = fake_toolchain.make_pe(filename="Core.dll", code_id=cid, debug_file="Core.pdb", debug_id=did)
    assert (await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb, "Core.pdb", pe, "Core.dll")).status_code == 201

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Core::AllocateBuffer Crash")
    resym = await client.post("/api/v1/symbols/resymbolicate", json={"project_id": proj["id"], "debug_id": did})
    assert resym.status_code == 202

    after = before
    for _ in range(150):
        cur = (await client.get(f"/api/v1/crashes/{crash_id}")).json()
        if cur["missing_symbols_json"] == [] and cur["status"] == "ANALYZED":
            after = cur
            break
        await asyncio.sleep(0.1)

    assert after["missing_symbols_json"] == []
    assert after["cluster"]["signature_hash"] != sig_before
    assert after["parsed_stacks_json"]["threads"][0]["frames"][0]["function"] == "Core::AllocateBuffer(size_t)"
    miss_after = (await client.get("/api/v1/symbols/missing", params={"project_id": proj["id"]})).json()
    assert not any(m["debug_id"] == did for m in miss_after)


async def test_t3_03_pdb_only_fallback_dedup_and_delete_disk_cleanup(
    client: Any,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-03-dedup-del")
    did = "330011112222333344445555666677771"
    pdb = fake_toolchain.make_pdb(filename="SoloMod.pdb", debug_id=did)

    r1 = await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb, "SoloMod.pdb")
    assert r1.status_code == 201
    assert r1.json()["has_pe_unwind"] is False
    assert r1.json()["deduplicated"] is False
    sym_id = r1.json()["id"]

    r2 = await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb, "SoloMod.pdb")
    assert r2.status_code == 201
    assert r2.json()["deduplicated"] is True
    assert (await client.get("/api/v1/symbols", params={"project_id": proj["id"]})).json()["total"] == 1

    sym_files = list((isolated_env["data_dir"] / "symbols" / "sym").rglob("*.sym"))
    assert len(sym_files) == 1

    d_resp = await client.delete(f"/api/v1/symbols/{sym_id}")
    assert d_resp.status_code == 204
    assert (await client.get("/api/v1/symbols", params={"project_id": proj["id"]})).json()["total"] == 0
    sym_files_after = list((isolated_env["data_dir"] / "symbols" / "sym").rglob("*.sym"))
    assert len(sym_files_after) == 0


async def test_t3_04_human_verdict_create_issue_feeds_smart_triage(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-04-kb-smart")
    modules = [
        {"filename": "ntdll.dll", "debug_file": "ntdll.pdb", "debug_id": "NT1", "always_loaded": True},
        {"filename": "App.exe", "debug_file": "App.pdb", "debug_id": "AP1", "always_loaded": True},
    ]
    dmp1 = fake_toolchain.make_dump(
        modules=modules,
        threads=[
            {
                "thread_name": "Main",
                "frames": [
                    {"module": "ntdll.dll", "function": "RaiseException", "offset": "0x1000"},
                    {"module": "App.exe", "function": "MeshLoader::ParseHeader()", "offset": "0x2000"},
                ],
            }
        ],
    )
    dmp2 = fake_toolchain.make_dump(
        modules=modules,
        threads=[
            {
                "thread_name": "Main",
                "frames": [
                    {"module": "ntdll.dll", "function": "KiUserExceptionDispatcher", "offset": "0x9000"},
                    {"module": "App.exe", "function": "MeshLoader::ParseHeader()", "offset": "0x7000"},
                ],
            }
        ],
    )

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Draft Mesh Crash")
    c1 = await helpers.wait_for_crash_status(
        client, (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp1)).json()["crash_id"]
    )
    assert len(c1["ai_runs"]) == 1

    v = await client.post(
        f"/api/v1/crashes/{c1['id']}/verdict",
        json={
            "action": "create_issue",
            "new_issue": {"title": "Confirmed Mesh Header Overflow", "component": "Assets"},
            "root_cause": "Unvalidated mesh vertex count",
            "add_log_patterns": ["MeshLoader.*overflow"],
            "is_ambiguous_stack": False,
        },
    )
    assert v.status_code == 200
    v_body = v.json()
    assert v_body["is_human_verified"] is True
    created_issue_id = v_body["issue"]["id"]

    mock_llm_server.reset()
    c2 = await helpers.wait_for_crash_status(
        client, (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp2)).json()["crash_id"]
    )
    assert c2["status"] == "ANALYZED"
    assert c2["cluster"]["id"] == c1["cluster"]["id"]
    assert c2["issue"]["id"] == created_issue_id
    assert c2["ai_runs"] == []
    assert len(mock_llm_server.requests) == 0

    issue_det = (await client.get(f"/api/v1/issues/{created_issue_id}")).json()
    assert issue_det["crash_count"] == 2


async def test_t3_05_fixed_issue_prefix_vs_postfix_regression(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-05-reg")
    dmp = fake_toolchain.make_dump()
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Renderer Leak")
    c1 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp, app_version="1.0.0")).json()["crash_id"],
    )
    issue_id = c1["issue"]["id"]
    await client.patch(
        f"/api/v1/issues/{issue_id}",
        json={"status": "FIXED", "fixed_in_version": "1.2.0", "is_ambiguous_stack": False},
    )

    # Pre-fix version 1.1.9 should NOT trigger REGRESSION
    mock_llm_server.reset()
    c_pre = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp, app_version="1.1.9")).json()["crash_id"],
    )
    assert c_pre["ai_verdict"] != "REGRESSION"
    issue_pre = (await client.get(f"/api/v1/issues/{issue_id}")).json()
    assert issue_pre["status"] == "FIXED"

    # Post-fix version 1.2.5 MUST trigger REGRESSION
    mock_llm_server.queue_verdict(verdict="REGRESSION", matched_issue_id=issue_id)
    c_post = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp, app_version="1.2.5")).json()["crash_id"],
    )
    assert c_post["ai_verdict"] == "REGRESSION"
    issue_post = (await client.get(f"/api/v1/issues/{issue_id}")).json()
    assert issue_post["status"] == "REGRESSION"
    assert issue_post["crash_count"] == 3


async def test_t3_06_ambiguous_stack_forces_ai_in_smart_triage(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-06-ambig")
    dmp = fake_toolchain.make_dump(
        threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": "Engine::FatalAssert()"}]}]
    )
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Generic Assert")
    c1 = await helpers.wait_for_crash_status(
        client, (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)).json()["crash_id"]
    )
    issue_id = c1["issue"]["id"]
    await client.patch(f"/api/v1/issues/{issue_id}", json={"is_ambiguous_stack": True})
    mock_llm_server.reset()

    log_bytes = b"2026-11-22 10:00:00.000 FATAL Disk full during asset save\n"
    mock_llm_server.queue_tool_call("get_crash_overview", {})
    mock_llm_server.queue_verdict(verdict="KNOWN_ISSUE", matched_issue_id=issue_id)
    c2 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp, log_files=[("a.log", log_bytes)])).json()["crash_id"],
    )
    assert len(c2["ai_runs"]) == 1
    assert c2["ai_runs"][0]["status"] == "COMPLETED"
    assert c2["ai_verdict"] == "KNOWN_ISSUE"
    assert c2["issue"]["id"] == issue_id
    assert len(mock_llm_server.requests) >= 1


async def test_t3_07_fuzzy_recall_top5_and_human_verdict(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-07-fuzzy")
    dmp1 = fake_toolchain.make_dump(
        threads=[
            {
                "thread_name": "M",
                "frames": [
                    {"module": "App.exe", "function": "Renderer::SubmitDrawCall(Mesh*)"},
                    {"module": "App.exe", "function": "Renderer::RenderScene()"},
                    {"module": "App.exe", "function": "App::Tick(float)"},
                ],
            }
        ]
    )
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="D3D11 Draw Pipeline Null Deref")
    c1 = await helpers.wait_for_crash_status(
        client, (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp1)).json()["crash_id"]
    )
    issue1_id = c1["issue"]["id"]

    # Shift top frame via inline helper so signature_hash differs, but fuzzy recall finds Issue 1
    dmp2 = fake_toolchain.make_dump(
        threads=[
            {
                "thread_name": "M",
                "frames": [
                    {"module": "App.exe", "function": "Renderer::InlineBindTexture()"},
                    {"module": "App.exe", "function": "Renderer::SubmitDrawCall(Mesh*)"},
                    {"module": "App.exe", "function": "Renderer::RenderScene()"},
                    {"module": "App.exe", "function": "App::Tick(float)"},
                ],
            }
        ]
    )

    captured_tool_out: list[str] = []

    def _handler(req_json: dict[str, Any], headers: dict[str, str], idx: int) -> dict[str, Any]:
        if idx == 0:
            return mock_llm_server._build_completion_payload(
                model=req_json["model"],
                tool_calls=mock_llm_server._make_tool_call_spec(
                    [("search_known_issues", {"query": ""})]
                )["tool_calls"],
                finish_reason="tool_calls",
            )
        tool_msgs = [m["content"] for m in req_json["messages"] if m.get("role") == "tool"]
        if tool_msgs:
            captured_tool_out.append(tool_msgs[-1])
        verdict = mock_llm_server._make_verdict_spec(verdict="KNOWN_ISSUE", matched_issue_id=issue1_id)
        return mock_llm_server._build_completion_payload(
            model=req_json["model"], content=verdict["content"], finish_reason="stop"
        )

    mock_llm_server.reset()
    mock_llm_server.set_handler(_handler)
    c2 = await helpers.wait_for_crash_status(
        client, (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp2)).json()["crash_id"]
    )
    assert c2["cluster"]["id"] != c1["cluster"]["id"]
    assert c2["ai_verdict"] == "KNOWN_ISSUE"
    assert c2["issue"]["id"] == issue1_id
    all_tool_out = "\n".join(captured_tool_out)
    assert str(issue1_id) in all_tool_out
    assert "D3D11 Draw Pipeline Null Deref" in all_tool_out

    v = await client.post(f"/api/v1/crashes/{c2['id']}/verdict", json={"action": "confirm"})
    assert v.status_code == 200
    assert v.json()["is_human_verified"] is True


async def test_t3_08_merge_split_and_dashboard_consistency(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-08-mrg-spl")
    dumps = [
        fake_toolchain.make_dump(threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": f"Fn{i}()"}]}])
        for i in range(3)
    ]
    crashes = []
    for i, d in enumerate(dumps):
        mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title=f"Bug {i}", key_log_patterns=[f"PAT_{i}"])
        crashes.append(
            await helpers.wait_for_crash_status(
                client, (await helpers.upload_crash(client, proj["key"], proj["api_key"], d)).json()["crash_id"]
            )
        )

    i0, i1, i2 = [c["issue"]["id"] for c in crashes]
    m_resp = await client.post("/api/v1/issues/merge", json={"target_issue_id": i0, "source_issue_ids": [i1, i2]})
    assert m_resp.status_code == 200
    assert m_resp.json()["crash_count"] == 3
    assert set(["PAT_0", "PAT_1", "PAT_2"]).issubset(set(m_resp.json()["log_patterns_json"]))

    s_resp = await client.post(
        f"/api/v1/issues/{i0}/split",
        json={"cluster_id": crashes[2]["cluster"]["id"], "new_issue_title": "Split Bug 2"},
    )
    assert s_resp.status_code == 200
    assert s_resp.json()["source_issue"]["crash_count"] == 2
    assert s_resp.json()["target_issue"]["crash_count"] == 1

    issues_listed = (await client.get("/api/v1/issues", params={"project_id": proj["id"]})).json()
    dash = (await client.get("/api/v1/dashboard", params={"project_id": proj["id"]})).json()
    assert dash["kpis"]["total_crashes"] == 3
    assert dash["kpis"]["open_issues"] == issues_listed["total"]


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
            threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": f"GcFn{idx}()"}]}]
        )
        up = await helpers.upload_crash(
            client, proj["key"], proj["api_key"], d, log_files=[(f"gc_{idx}.log", helpers.make_mixed_encoding_log(15))]
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
    assert (await client.get(f"/api/v1/crashes/{c3['id']}/artifacts/dump")).status_code in (404, 410)
    assert (await client.get(f"/api/v1/logs/{c3['logs'][0]['id']}/lines", params={"start": 1, "end": 5})).status_code in (404, 410)
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
            {"filename": "wrapper.dll", "debug_file": "wrapper.pdb", "debug_id": "W1", "always_loaded": True},
            {"filename": "App.exe", "debug_file": "App.pdb", "debug_id": "A1", "always_loaded": True},
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
        client, (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)).json()["crash_id"]
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
        client, (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)).json()["crash_id"]
    )
    assert len(c2["ai_runs"]) == 1
    assert c2["ai_runs"][0]["model_name"] == "custom-gpt"
    assert c2["ai_verdict"] == "NEEDS_REVIEW"
    assert all(r["body"]["model"] == "custom-gpt" for r in mock_llm_server.requests)


def test_t3_11_project_key_auth_and_rotation_http_and_cli(
    live_server: str,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    import httpx

    proj = httpx.post(
        f"{live_server}/api/v1/projects",
        json={"key": "t3-11-rot", "name": "Rotate HTTP CLI"},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()
    old_key = proj["api_key"]
    dmp_bytes = fake_toolchain.make_dump()
    dmp_path = tmp_path / "rot.dmp"
    dmp_path.write_bytes(dmp_bytes)

    assert (
        httpx.post(
            f"{live_server}/api/v1/crashes/upload",
            data={"project_key": proj["key"], "app_version": "1.0.0"},
            files={"dump_file": ("rot.dmp", dmp_bytes, "application/octet-stream")},
            timeout=10.0,
        ).status_code
        == 401
    )
    assert (
        httpx.post(
            f"{live_server}/api/v1/crashes/upload",
            data={"project_key": proj["key"], "app_version": "1.0.0"},
            headers={"X-API-Key": "wrong"},
            files={"dump_file": ("rot.dmp", dmp_bytes, "application/octet-stream")},
            timeout=10.0,
        ).status_code
        == 403
    )

    new_key = httpx.post(
        f"{live_server}/api/v1/projects/{proj['id']}/rotate-key",
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()["api_key"]
    assert new_key != old_key

    cli_old = helpers.run_cli(
        [
            "upload-crash",
            "--server", live_server,
            "--project", proj["key"],
            "--api-key", old_key,
            "--dump", str(dmp_path),
            "--app-version", "1.0.0",
        ],
        env=isolated_env["proc_env"],
    )
    assert cli_old.returncode != 0

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Rotated Key Crash")
    cli_new = helpers.run_cli(
        [
            "upload-crash",
            "--server", live_server,
            "--project", proj["key"],
            "--api-key", new_key,
            "--dump", str(dmp_path),
            "--app-version", "1.0.0",
            "--wait",
        ],
        env=isolated_env["proc_env"],
    )
    assert cli_new.returncode == 0


async def test_t3_12_cli_upload_symbols_and_crash_sse_dashboard(
    live_server: str,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    import httpx

    async with httpx.AsyncClient(
        base_url=live_server, timeout=20.0, auth=isolated_env["admin_auth"]
    ) as hc:
        proj = (await hc.post("/api/v1/projects", json={"key": "t3-12-cli", "name": "CLI SSE Dash"})).json()
        build_dir = tmp_path / "cli_build"
        did = "312011112222333344445555666677771"
        fake_toolchain.make_pe(filename="CliApp.exe", code_id="C120", debug_file="CliApp.pdb", debug_id=did, write_to=build_dir / "CliApp.exe")
        fake_toolchain.make_pdb(filename="CliApp.pdb", debug_id=did, code_file="CliApp.exe", code_id="C120", write_to=build_dir / "CliApp.pdb")

        sym_res = await asyncio.to_thread(
            helpers.run_cli,
            [
                "upload-symbols",
                "--server", live_server,
                "--project", proj["key"],
                "--api-key", proj["api_key"],
                "--dir", str(build_dir),
                "--app-version", "2.0.0",
            ],
            isolated_env["proc_env"],
        )
        assert sym_res.returncode == 0

        dmp_path = tmp_path / "cli_app.dmp"
        fake_toolchain.make_dump(
            modules=[{"filename": "CliApp.exe", "debug_file": "CliApp.pdb", "debug_id": did, "code_id": "C120", "always_loaded": False}],
            write_to=dmp_path,
        )
        mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="CLI SSE Bug")
        crash_res = await asyncio.to_thread(
            helpers.run_cli,
            [
                "upload-crash",
                "--server", live_server,
                "--project", proj["key"],
                "--api-key", proj["api_key"],
                "--dump", str(dmp_path),
                "--app-version", "2.0.0",
                "--wait",
            ],
            isolated_env["proc_env"],
        )
        assert crash_res.returncode == 0
        assert "ANALYZED" in (crash_res.stdout + crash_res.stderr)

        crashes = (await hc.get("/api/v1/crashes", params={"project_id": proj["id"]})).json()["items"]
        assert len(crashes) == 1
        events = await helpers.stream_crash_sse(hc, crashes[0]["id"])
        assert events[-1][1]["status"] == "ANALYZED"

        detail = (await hc.get(f"/api/v1/crashes/{crashes[0]['id']}")).json()
        assert detail["missing_symbols_json"] == []
        assert detail["parsed_stacks_json"]["threads"][0]["frames"][0]["trust"] == "cfi"

        dash = (await hc.get("/api/v1/dashboard", params={"project_id": proj["id"]})).json()
        assert dash["kpis"]["total_crashes"] == 1
        assert dash["kpis"]["analyzed_crashes"] == 1


async def test_t3_13_multilog_upload_log_apis_and_ai_evidence(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-13-multilog")
    app_log = (
        "2026-11-22 10:00:00.000 INFO boot ok\n"
        "2026-11-22 10:00:10.000 ERROR Root cause pointer null at 0x0\n"
        "2026-11-22 10:00:20.000 FATAL Abort after null pointer\n"
    ).encode("utf-8")
    net_log = "2026-11-22 10:00:09.000 WARN 网络连接超时\n".encode("gbk")

    def _handler(req_json: dict[str, Any], headers: dict[str, str], idx: int) -> dict[str, Any]:
        if idx == 0:
            return mock_llm_server._build_completion_payload(
                model=req_json["model"],
                tool_calls=mock_llm_server._make_tool_call_spec([("get_crash_overview", {})])["tool_calls"],
                finish_reason="tool_calls",
            )
        tool_msgs = [m["content"] for m in req_json["messages"] if m.get("role") == "tool"]
        lid = helpers.extract_log_id(tool_msgs[0], preferred_filename="app.log")
        verdict = mock_llm_server._make_verdict_spec(
            verdict="NEW_ISSUE",
            suggested_title="Null Pointer with Log Evidence",
            evidence_log_snippets=[{"log_id": lid, "start_line": 2, "end_line": 3, "relevance": "null ptr"}],
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
        log_files=[("app.log", app_log), ("net.log", net_log)],
        crash_time="2026-11-22T10:00:15Z",
    )
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    assert len(detail["logs"]) == 2
    app_lid, net_lid = up.json()["log_ids"]

    summary = (await client.get(f"/api/v1/logs/{app_lid}/summary")).json()
    assert len(summary["digest"]["recent_errors"]) >= 1

    around = (
        await client.get(f"/api/v1/logs/{app_lid}/around-time", params={"timestamp": "2026-11-22T10:00:15", "window_sec": 6})
    ).json()
    assert any("Root cause pointer null" in l["text"] for l in around["lines"])

    net_lines = (await client.get(f"/api/v1/logs/{net_lid}/lines", params={"start": 1, "end": 5})).json()["lines"]
    assert "网络连接超时" in net_lines[0]["text"]

    snip = detail["ai_runs"][0]["result_json"]["evidence_log_snippets"][0]
    assert snip["log_id"] == app_lid
    ev_lines = (
        await client.get(f"/api/v1/logs/{snip['log_id']}/lines", params={"start": snip["start_line"], "end": snip["end_line"]})
    ).json()["lines"]
    assert any("Root cause pointer null" in l["text"] for l in ev_lines)


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
            {"filename": "custom_wrapper.dll", "debug_file": "cw.pdb", "debug_id": "CW1", "always_loaded": True},
            {"filename": "App.exe", "debug_file": "App.pdb", "debug_id": "AP1", "always_loaded": True},
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
        client, (await helpers.upload_crash(client, proj_a["key"], proj_a["api_key"], dmp)).json()["crash_id"]
    )
    assert c_a["ai_runs"] == []
    assert "custom_wrapper.dll" not in str(c_a["cluster"]["signature_frames_json"])

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Proj B Crash")
    c_b = await helpers.wait_for_crash_status(
        client, (await helpers.upload_crash(client, proj_b["key"], proj_b["api_key"], dmp)).json()["crash_id"]
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
        modules=[{"filename": "Mod.dll", "debug_file": "Mod.pdb", "debug_id": "ISO111", "always_loaded": False}],
        threads=[{"thread_name": "M", "frames": [{"module": "Mod.dll", "function": "F()"}]}],
    )
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="P1 Issue")
    c1 = await helpers.wait_for_crash_status(
        client, (await helpers.upload_crash(client, p1["key"], p1["api_key"], dmp)).json()["crash_id"]
    )
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="P2 Issue")
    c2 = await helpers.wait_for_crash_status(
        client, (await helpers.upload_crash(client, p2["key"], p2["api_key"], dmp)).json()["crash_id"]
    )

    assert c1["cluster"]["signature_hash"] != c2["cluster"]["signature_hash"]
    assert (await helpers.upload_crash(client, p2["key"], p1["api_key"], dmp)).status_code == 403
    assert (
        await client.post("/api/v1/issues/merge", json={"target_issue_id": c1["issue"]["id"], "source_issue_ids": [c2["issue"]["id"]]})
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
    await client.put("/api/v1/settings", json={"archive_raw_symbols": True, "host_safety_floor_gb": 50.0})

    mock_llm_server.queue_verdict()
    c_pin = (
        await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump(extra_bytes=b"PIN"))
    ).json()["crash_id"]
    await helpers.wait_for_crash_status(client, c_pin)
    await client.patch(f"/api/v1/crashes/{c_pin}/pin", json={"is_pinned": True})

    mock_llm_server.queue_verdict()
    c_unpin = (
        await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump(extra_bytes=b"UNPIN"))
    ).json()["crash_id"]
    await helpers.wait_for_crash_status(client, c_unpin)

    fake_disk_usage.set_free_gb(40.0)
    did = "316011112222333344445555666677771"
    sym = await helpers.upload_symbol(
        client, proj["key"], proj["api_key"], fake_toolchain.make_pdb(filename="S16.pdb", debug_id=did), "S16.pdb"
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

    assert (await client.get(f"/api/v1/crashes/{crash_id}/artifacts/dump")).status_code in (404, 410)
    assert (await client.get(f"/api/v1/logs/{log_id}/lines", params={"start": 1, "end": 5})).status_code in (404, 410)
    assert (await client.post(f"/api/v1/crashes/{crash_id}/reprocess", json={"mode": "full"})).status_code == 409

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
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump())
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


async def test_t3_19_sse_corrupt_dump_vs_late_connect(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-19-sse")
    bad_up = await helpers.upload_crash(client, proj["key"], proj["api_key"], b"BAD_DMP_HEADER")
    bad_id = bad_up.json()["crash_id"]
    bad_events = await helpers.stream_crash_sse(client, bad_id)
    assert bad_events[-1][1]["status"] == "FAILED"
    assert bad_events[-1][1]["error"]

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Good One")
    good_up = await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump())
    good_id = good_up.json()["crash_id"]
    await helpers.wait_for_crash_status(client, good_id)
    late_events = await helpers.stream_crash_sse(client, good_id, timeout=5.0)
    assert late_events[-1][1]["status"] == "ANALYZED"
    assert late_events[-1][1]["ai_verdict"] == "NEW_ISSUE"


async def test_t3_20_mixed_encoding_log_via_cli_and_log_apis(
    live_server: str,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    import httpx

    async with httpx.AsyncClient(
        base_url=live_server, timeout=20.0, auth=isolated_env["admin_auth"]
    ) as hc:
        proj = (await hc.post("/api/v1/projects", json={"key": "t3-20-cli-enc", "name": "CLI Enc"})).json()
        dmp_path = tmp_path / "enc.dmp"
        fake_toolchain.make_dump(write_to=dmp_path)
        log_path = tmp_path / "enc_mixed.log"
        log_path.write_bytes(
            helpers.make_mixed_encoding_log(num_lines=40, include_bom=True, include_gbk=True, include_invalid_bytes=True)
        )

        mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Mixed Log Crash")
        res = await asyncio.to_thread(
            helpers.run_cli,
            [
                "upload-crash",
                "--server", live_server,
                "--project", proj["key"],
                "--api-key", proj["api_key"],
                "--dump", str(dmp_path),
                "--log", str(log_path),
                "--app-version", "1.0.0",
                "--wait",
            ],
            isolated_env["proc_env"],
        )
        assert res.returncode == 0

        crashes = (await hc.get("/api/v1/crashes", params={"project_id": proj["id"]})).json()["items"]
        detail = (await hc.get(f"/api/v1/crashes/{crashes[0]['id']}")).json()
        lid = detail["logs"][0]["id"]

        sr = (
            await hc.get(f"/api/v1/logs/{lid}/search", params={"pattern": "纹理.*失败", "is_regex": "true"})
        ).json()
        assert sr["total_matches"] >= 1
        assert sr["elapsed_ms"] >= 0
        assert (await hc.get(f"/api/v1/logs/{lid}/search", params={"pattern": "[unclosed", "is_regex": "true"})).status_code in (400, 422)


async def test_t3_21_dashboard_aggregations_accuracy(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-21-dash-acc")
    # 1 corrupt -> FAILED
    u_fail = await helpers.upload_crash(client, proj["key"], proj["api_key"], b"CORRUPT")
    await helpers.wait_for_crash_status(client, u_fail.json()["crash_id"], target_statuses=("FAILED",))

    # 1 missing symbol -> NEW_ISSUE
    d_miss = fake_toolchain.make_dump(
        modules=[{"filename": "M21.dll", "debug_file": "M21.pdb", "debug_id": "M21ID", "always_loaded": False}],
        threads=[{"thread_name": "M", "frames": [{"module": "M21.dll", "function": "X()"}]}],
    )
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Missing M21")
    u_miss = await helpers.upload_crash(client, proj["key"], proj["api_key"], d_miss)
    await helpers.wait_for_crash_status(client, u_miss.json()["crash_id"])

    # 1 timeout -> NEEDS_REVIEW
    await client.put("/api/v1/settings", json={"ai_timeout_seconds": 1})
    mock_llm_server.set_delay(2.5)
    d_nr = fake_toolchain.make_dump(threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": "Slow()"}]}])
    u_nr = await helpers.upload_crash(client, proj["key"], proj["api_key"], d_nr)
    await helpers.wait_for_crash_status(client, u_nr.json()["crash_id"])

    dash = (await client.get("/api/v1/dashboard", params={"project_id": proj["id"], "days": 14})).json()
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
    await client.put("/api/v1/settings", json={"ai_tool_output_max_chars": 400, "ai_max_steps": 3, "ai_triage_mode": "ALWAYS"})
    proj = await helpers.create_project(client, key="t3-22-guard")
    big_log = "".join(f"2026-11-22 10:00:00.000 INFO line_{i}_" + ("B" * 100) + "\n" for i in range(40)).encode("utf-8")

    expected_log_id: list[str] = []

    def _loop_handler(req_json: dict[str, Any], headers: dict[str, str], idx: int) -> dict[str, Any]:
        if idx == 0:
            return mock_llm_server._build_completion_payload(
                model=req_json["model"],
                tool_calls=mock_llm_server._make_tool_call_spec([("get_crash_overview", {})])["tool_calls"],
                finish_reason="tool_calls",
            )
        tool_msgs = [m for m in req_json["messages"] if m.get("role") == "tool"]
        lid = helpers.extract_log_id(tool_msgs[0]["content"]) or (expected_log_id[0] if expected_log_id else "")
        return mock_llm_server._build_completion_payload(
            model=req_json["model"],
            tool_calls=mock_llm_server._make_tool_call_spec(
                [("read_log_lines", {"log_id": lid, "start_line": 1, "end_line": 40})]
            )["tool_calls"],
            finish_reason="tool_calls",
        )

    mock_llm_server.set_handler(_loop_handler)
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump(), log_files=[("b.log", big_log)]
    )
    expected_log_id.append(up.json()["log_ids"][0])
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    assert detail["ai_verdict"] == "NEEDS_REVIEW"
    run = detail["ai_runs"][0]
    assert len(run["trace_json"]) <= 4
    third_tool_msgs = [m for m in mock_llm_server.requests[2]["body"]["messages"] if m.get("role") == "tool"]
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
        await client.post("/api/v1/issues", json={"project_id": proj["id"], "title": "Manual Target", "status": "KNOWN", "component": "Net"})
    ).json()

    crashes = []
    for idx in range(3):
        mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title=f"Auto {idx}")
        d = fake_toolchain.make_dump(threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": f"VFn{idx}()"}]}])
        up = await helpers.upload_crash(client, proj["key"], proj["api_key"], d)
        crashes.append(await helpers.wait_for_crash_status(client, up.json()["crash_id"]))

    await client.post(f"/api/v1/crashes/{crashes[0]['id']}/verdict", json={"action": "confirm"})
    await client.post(f"/api/v1/crashes/{crashes[1]['id']}/verdict", json={"action": "assign_issue", "issue_id": manual_issue["id"]})
    await client.post(
        f"/api/v1/crashes/{crashes[2]['id']}/verdict",
        json={"action": "create_issue", "new_issue": {"title": "Created From Verdict", "component": "UI"}},
    )

    verified = (
        await client.get("/api/v1/crashes", params={"project_id": proj["id"], "is_human_verified": "true"})
    ).json()
    assert verified["total"] == 3

    net_issues = (
        await client.get("/api/v1/issues", params={"project_id": proj["id"], "status": "KNOWN", "component": "Net"})
    ).json()
    assert net_issues["total"] == 1 and net_issues["items"][0]["crash_count"] == 1

    clusters = (await client.get("/api/v1/issues/clusters", params={"project_id": proj["id"], "unassigned_only": "false"})).json()
    assert clusters["total"] == 3


def test_t3_24_cli_upload_symbols_pairing_dry_run_and_errors(
    live_server: str,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    import httpx

    proj = httpx.post(
        f"{live_server}/api/v1/projects",
        json={"key": "t3-24-cli", "name": "CLI Sym"},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()
    bdir = tmp_path / "sym_bundle"
    fake_toolchain.make_pe("a.exe", "CA", "a.pdb", "DA1111111111111111111111111111111", write_to=bdir / "a.exe")
    fake_toolchain.make_pdb("a.pdb", "DA1111111111111111111111111111111", "a.exe", "CA", write_to=bdir / "a.pdb")
    fake_toolchain.make_pe("b.dll", "CB", "b.pdb", "DB1111111111111111111111111111111", write_to=bdir / "sub" / "b.dll")
    fake_toolchain.make_pdb("b.pdb", "DB1111111111111111111111111111111", "b.dll", "CB", write_to=bdir / "sub" / "b.pdb")
    fake_toolchain.make_pdb("c.pdb", "DC1111111111111111111111111111111", "c.dll", "CC", write_to=bdir / "c.pdb")
    fake_toolchain.make_pe("unpaired.exe", "CU", "unpaired.pdb", "DU1111", write_to=bdir / "unpaired.exe")

    dry = helpers.run_cli(
        [
            "upload-symbols", "--server", live_server, "--project", proj["key"], "--api-key", proj["api_key"],
            "--dir", str(bdir), "--app-version", "1.0.0", "--dry-run",
        ],
        env=isolated_env["proc_env"],
    )
    assert dry.returncode == 0
    snapshots = httpx.get(
        f"{live_server}/api/v1/symbols",
        params={"project_id": proj["id"]},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()
    assert snapshots["total"] == 0

    real = helpers.run_cli(
        [
            "upload-symbols", "--server", live_server, "--project", proj["key"], "--api-key", proj["api_key"],
            "--dir", str(bdir), "--app-version", "1.0.0",
        ],
        env=isolated_env["proc_env"],
    )
    assert real.returncode == 0
    items = httpx.get(
        f"{live_server}/api/v1/symbols",
        params={"project_id": proj["id"]},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()["items"]
    assert len(items) == 3
    by_name = {s["debug_file"]: s for s in items}
    assert by_name["a.pdb"]["has_pe_unwind"] is True
    assert by_name["b.pdb"]["has_pe_unwind"] is True
    assert by_name["c.pdb"]["has_pe_unwind"] is False

    bad = helpers.run_cli(
        [
            "upload-symbols", "--server", live_server, "--project", proj["key"], "--api-key", "bad-key",
            "--dir", str(bdir), "--app-version", "1.0.0", "--fail-on-error",
        ],
        env=isolated_env["proc_env"],
    )
    assert bad.returncode != 0
