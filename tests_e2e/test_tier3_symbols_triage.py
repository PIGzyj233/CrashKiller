from __future__ import annotations

import asyncio
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
    assert (
        await client.get("/symsrv/GameEngine.pdb/NONEXISTENT/GameEngine.pdb")
    ).status_code == 404

    syms_after = (await client.get("/api/v1/symbols", params={"project_id": proj["id"]})).json()[
        "items"
    ]
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
                    {
                        "module": "Core.dll",
                        "function": "Core::AllocateBuffer(size_t)",
                        "file": "src/mem.cpp",
                        "line": 88,
                    }
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

    miss_list = (
        await client.get("/api/v1/symbols/missing", params={"project_id": proj["id"]})
    ).json()
    assert any(m["debug_id"] == did and m["crash_count"] == 1 for m in miss_list)

    pdb = fake_toolchain.make_pdb(
        filename="Core.pdb", debug_id=did, code_file="Core.dll", code_id=cid
    )
    pe = fake_toolchain.make_pe(
        filename="Core.dll", code_id=cid, debug_file="Core.pdb", debug_id=did
    )
    assert (
        await helpers.upload_symbol(
            client, proj["key"], proj["api_key"], pdb, "Core.pdb", pe, "Core.dll"
        )
    ).status_code == 201

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Core::AllocateBuffer Crash")
    resym = await client.post(
        "/api/v1/symbols/resymbolicate", json={"project_id": proj["id"], "debug_id": did}
    )
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
    assert (
        after["parsed_stacks_json"]["threads"][0]["frames"][0]["function"]
        == "Core::AllocateBuffer(size_t)"
    )
    miss_after = (
        await client.get("/api/v1/symbols/missing", params={"project_id": proj["id"]})
    ).json()
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
    assert (await client.get("/api/v1/symbols", params={"project_id": proj["id"]})).json()[
        "total"
    ] == 1

    sym_files = list((isolated_env["data_dir"] / "symbols" / "sym").rglob("*.sym"))
    assert len(sym_files) == 1

    d_resp = await client.delete(f"/api/v1/symbols/{sym_id}")
    assert d_resp.status_code == 204
    assert (await client.get("/api/v1/symbols", params={"project_id": proj["id"]})).json()[
        "total"
    ] == 0
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
        {
            "filename": "ntdll.dll",
            "debug_file": "ntdll.pdb",
            "debug_id": "NT1",
            "always_loaded": True,
        },
        {"filename": "App.exe", "debug_file": "App.pdb", "debug_id": "AP1", "always_loaded": True},
    ]
    dmp1 = fake_toolchain.make_dump(
        modules=modules,
        threads=[
            {
                "thread_name": "Main",
                "frames": [
                    {"module": "ntdll.dll", "function": "RaiseException", "offset": "0x1000"},
                    {
                        "module": "App.exe",
                        "function": "MeshLoader::ParseHeader()",
                        "offset": "0x2000",
                    },
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
                    {
                        "module": "ntdll.dll",
                        "function": "KiUserExceptionDispatcher",
                        "offset": "0x9000",
                    },
                    {
                        "module": "App.exe",
                        "function": "MeshLoader::ParseHeader()",
                        "offset": "0x7000",
                    },
                ],
            }
        ],
    )

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Draft Mesh Crash")
    c1 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp1)).json()["crash_id"],
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
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp2)).json()["crash_id"],
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
        (
            await helpers.upload_crash(
                client, proj["key"], proj["api_key"], dmp, app_version="1.0.0"
            )
        ).json()["crash_id"],
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
        (
            await helpers.upload_crash(
                client, proj["key"], proj["api_key"], dmp, app_version="1.1.9"
            )
        ).json()["crash_id"],
    )
    assert c_pre["ai_verdict"] != "REGRESSION"
    issue_pre = (await client.get(f"/api/v1/issues/{issue_id}")).json()
    assert issue_pre["status"] == "FIXED"

    # Post-fix version 1.2.5 MUST trigger REGRESSION
    mock_llm_server.queue_verdict(verdict="REGRESSION", matched_issue_id=issue_id)
    c_post = await helpers.wait_for_crash_status(
        client,
        (
            await helpers.upload_crash(
                client, proj["key"], proj["api_key"], dmp, app_version="1.2.5"
            )
        ).json()["crash_id"],
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
        threads=[
            {
                "thread_name": "M",
                "frames": [{"module": "App.exe", "function": "Engine::FatalAssert()"}],
            }
        ]
    )
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Generic Assert")
    c1 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)).json()["crash_id"],
    )
    issue_id = c1["issue"]["id"]
    await client.patch(f"/api/v1/issues/{issue_id}", json={"is_ambiguous_stack": True})
    mock_llm_server.reset()

    log_bytes = b"2026-11-22 10:00:00.000 FATAL Disk full during asset save\n"
    mock_llm_server.queue_tool_call("get_crash_overview", {})
    mock_llm_server.queue_verdict(verdict="KNOWN_ISSUE", matched_issue_id=issue_id)
    c2 = await helpers.wait_for_crash_status(
        client,
        (
            await helpers.upload_crash(
                client, proj["key"], proj["api_key"], dmp, log_files=[("a.log", log_bytes)]
            )
        ).json()["crash_id"],
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
    mock_llm_server.queue_verdict(
        verdict="NEW_ISSUE", suggested_title="D3D11 Draw Pipeline Null Deref"
    )
    c1 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp1)).json()["crash_id"],
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
                    [("get_crash_overview", {}), ("search_known_issues", {"query": ""})]
                )["tool_calls"],
                finish_reason="tool_calls",
            )
        tool_msgs = [m["content"] for m in req_json["messages"] if m.get("role") == "tool"]
        if tool_msgs:
            captured_tool_out.append(tool_msgs[-1])
        verdict = mock_llm_server._make_verdict_spec(
            verdict="KNOWN_ISSUE", matched_issue_id=issue1_id
        )
        return mock_llm_server._build_completion_payload(
            model=req_json["model"], content=verdict["content"], finish_reason="stop"
        )

    mock_llm_server.reset()
    mock_llm_server.set_handler(_handler)
    c2 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp2)).json()["crash_id"],
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
        fake_toolchain.make_dump(
            threads=[
                {"thread_name": "M", "frames": [{"module": "App.exe", "function": f"Fn{i}()"}]}
            ]
        )
        for i in range(3)
    ]
    crashes = []
    for i, d in enumerate(dumps):
        mock_llm_server.queue_verdict(
            verdict="NEW_ISSUE", suggested_title=f"Bug {i}", key_log_patterns=[f"PAT_{i}"]
        )
        crashes.append(
            await helpers.wait_for_crash_status(
                client,
                (await helpers.upload_crash(client, proj["key"], proj["api_key"], d)).json()[
                    "crash_id"
                ],
            )
        )

    i0, i1, i2 = [c["issue"]["id"] for c in crashes]
    m_resp = await client.post(
        "/api/v1/issues/merge", json={"target_issue_id": i0, "source_issue_ids": [i1, i2]}
    )
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
