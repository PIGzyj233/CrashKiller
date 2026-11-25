from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.tier1


# ---------------------------------------------------------------------------
# Area 6: Clustering, Issues & Human Verdict KB (T1-30 .. T1-36)
# ---------------------------------------------------------------------------


async def test_t1_30_deterministic_clustering_across_noise_and_addresses(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="cluster-det")
    modules = [
        {
            "filename": "ntdll.dll",
            "debug_file": "ntdll.pdb",
            "debug_id": "NTDLL1",
            "always_loaded": True,
        },
        {
            "filename": "kernelbase.dll",
            "debug_file": "kernelbase.pdb",
            "debug_id": "KBASE1",
            "always_loaded": True,
        },
        {
            "filename": "MyApp.exe",
            "debug_file": "MyApp.pdb",
            "debug_id": "MYAPP1",
            "always_loaded": True,
        },
    ]
    dmp1 = fake_toolchain.make_dump(
        modules=modules,
        threads=[
            {
                "thread_name": "Main",
                "frames": [
                    {
                        "module": "ntdll.dll",
                        "function": "KiUserExceptionDispatcher",
                        "offset": "0x7ff900001000",
                    },
                    {
                        "module": "kernelbase.dll",
                        "function": "RaiseException",
                        "offset": "0x7ff900002000",
                    },
                    {
                        "module": "MyApp.exe",
                        "function": "Renderer::SubmitDrawCall(Mesh*)",
                        "offset": "0x7ff700011000",
                    },
                    {
                        "module": "MyApp.exe",
                        "function": "App::Tick(float)",
                        "offset": "0x7ff700012000",
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
                        "function": "RtlDispatchException",
                        "offset": "0x7ff900009999",
                    },
                    {
                        "module": "MyApp.exe",
                        "function": "Renderer::SubmitDrawCall(Mesh*)",
                        "offset": "0x7ff700099000",
                    },
                    {
                        "module": "MyApp.exe",
                        "function": "App::Tick(float)",
                        "offset": "0x7ff70009a000",
                    },
                ],
            }
        ],
    )
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="SubmitDrawCall Crash")
    u1 = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp1)
    c1 = await helpers.wait_for_crash_status(client, u1.json()["crash_id"])

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="SubmitDrawCall Crash")
    u2 = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp2)
    c2 = await helpers.wait_for_crash_status(client, u2.json()["crash_id"])

    assert c1["cluster"]["id"] == c2["cluster"]["id"]
    assert c2["cluster"]["crash_count"] == 2


async def test_t1_31_create_and_list_issues(client: Any, helpers: Any) -> None:
    proj = await helpers.create_project(client, key="issue-crud")
    cr = await client.post(
        "/api/v1/issues",
        json={
            "project_id": proj["id"],
            "title": "Null Texture",
            "status": "KNOWN",
            "component": "Rendering",
            "log_patterns_json": ["Texture.*null"],
        },
    )
    assert cr.status_code == 201
    issue = cr.json()
    assert issue["title"] == "Null Texture"
    assert issue["status"] == "KNOWN"

    ls = await client.get(
        "/api/v1/issues",
        params={
            "project_id": proj["id"],
            "status": "KNOWN",
            "component": "Rendering",
            "q": "Texture",
        },
    )
    assert ls.status_code == 200
    body = ls.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == issue["id"]


async def test_t1_32_get_and_update_issue(client: Any, helpers: Any) -> None:
    proj = await helpers.create_project(client, key="issue-upd")
    cr = await client.post(
        "/api/v1/issues",
        json={"project_id": proj["id"], "title": "Init Issue", "status": "NEW"},
    )
    issue_id = cr.json()["id"]

    pt = await client.patch(
        f"/api/v1/issues/{issue_id}",
        json={
            "status": "FIXED",
            "fixed_in_version": "1.2.0",
            "root_cause": "Missing null check",
            "is_ambiguous_stack": True,
            "is_pinned": True,
        },
    )
    assert pt.status_code == 200

    det = await client.get(f"/api/v1/issues/{issue_id}")
    assert det.status_code == 200
    body = det.json()
    assert body["status"] == "FIXED"
    assert body["fixed_in_version"] == "1.2.0"
    assert body["root_cause"] == "Missing null check"
    assert body["is_ambiguous_stack"] is True
    assert body["is_pinned"] is True
    assert "clusters" in body and "recent_crashes" in body


async def test_t1_33_list_clusters(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="cluster-list")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump()
    )
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    resp = await client.get(
        "/api/v1/issues/clusters",
        params={"project_id": proj["id"], "unassigned_only": "false"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    cl = body["items"][0]
    assert "signature_hash" in cl
    assert "signature_frames_json" in cl
    assert cl["crash_count"] >= 1


async def test_t1_34_merge_issues(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="issue-merge")
    d1 = fake_toolchain.make_dump(
        threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": "FuncA()"}]}]
    )
    d2 = fake_toolchain.make_dump(
        threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": "FuncB()"}]}]
    )

    mock_llm_server.queue_verdict(
        verdict="NEW_ISSUE", suggested_title="Issue A", key_log_patterns=["PAT_A"]
    )
    u1 = await helpers.upload_crash(client, proj["key"], proj["api_key"], d1)
    c1 = await helpers.wait_for_crash_status(client, u1.json()["crash_id"])

    mock_llm_server.queue_verdict(
        verdict="NEW_ISSUE", suggested_title="Issue B", key_log_patterns=["PAT_B"]
    )
    u2 = await helpers.upload_crash(client, proj["key"], proj["api_key"], d2)
    c2 = await helpers.wait_for_crash_status(client, u2.json()["crash_id"])

    target_id = c1["issue"]["id"]
    src_id = c2["issue"]["id"]
    assert target_id != src_id

    m = await client.post(
        "/api/v1/issues/merge", json={"target_issue_id": target_id, "source_issue_ids": [src_id]}
    )
    assert m.status_code == 200
    merged = m.json()
    assert merged["crash_count"] == 2
    assert len(merged["clusters"]) == 2
    assert set(["PAT_A", "PAT_B"]).issubset(set(merged["log_patterns_json"]))
    assert (await client.get(f"/api/v1/issues/{src_id}")).status_code == 404


async def test_t1_35_split_cluster_from_issue(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="issue-split")
    d1 = fake_toolchain.make_dump(
        threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": "SplitA()"}]}]
    )
    d2 = fake_toolchain.make_dump(
        threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": "SplitB()"}]}]
    )

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Issue 1")
    c1 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], d1)).json()["crash_id"],
    )
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Issue 2")
    c2 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], d2)).json()["crash_id"],
    )

    t_id = c1["issue"]["id"]
    s_id = c2["issue"]["id"]
    await client.post(
        "/api/v1/issues/merge", json={"target_issue_id": t_id, "source_issue_ids": [s_id]}
    )

    split_resp = await client.post(
        f"/api/v1/issues/{t_id}/split",
        json={"cluster_id": c2["cluster"]["id"], "new_issue_title": "Split Issue"},
    )
    assert split_resp.status_code == 200
    body = split_resp.json()
    assert body["source_issue"]["crash_count"] == 1
    assert body["target_issue"]["title"] == "Split Issue"
    assert body["target_issue"]["crash_count"] == 1
    assert body["cluster"]["issue_id"] == body["target_issue"]["id"]


async def test_t1_36_human_verdict_feedback(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="verdict-kb")
    target_issue = (
        await client.post(
            "/api/v1/issues",
            json={"project_id": proj["id"], "title": "KB Target", "status": "KNOWN"},
        )
    ).json()
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Initial Wrong Issue")
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump()
    )
    crash_id = up.json()["crash_id"]
    await helpers.wait_for_crash_status(client, crash_id)

    v_resp = await client.post(
        f"/api/v1/crashes/{crash_id}/verdict",
        json={
            "action": "assign_issue",
            "issue_id": target_issue["id"],
            "root_cause": "Verified RC",
            "add_log_patterns": ["ERR_PTR"],
            "is_ambiguous_stack": False,
        },
    )
    assert v_resp.status_code == 200
    v_data = v_resp.json()
    assert v_data["is_human_verified"] is True
    assert v_data["issue"]["id"] == target_issue["id"]

    issue_after = (await client.get(f"/api/v1/issues/{target_issue['id']}")).json()
    assert issue_after["root_cause"] == "Verified RC"
    assert "ERR_PTR" in issue_after["log_patterns_json"]


# ---------------------------------------------------------------------------
# Area 7: AI Triage (T1-37 .. T1-41)
# ---------------------------------------------------------------------------


async def test_t1_37_ai_multiturn_react_loop_and_trace(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    await client.put("/api/v1/settings", json={"ai_triage_mode": "ALWAYS"})
    proj = await helpers.create_project(client, key="ai-react")
    log_bytes = helpers.make_mixed_encoding_log(
        num_lines=40,
        error_lines={22: "FATAL TextureCache::Get null pointer deref"},
    )

    def _dynamic_handler(
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
        tool_msgs = [m["content"] for m in req_json["messages"] if m.get("role") == "tool"]
        lid = helpers.extract_log_id(tool_msgs[0])
        if idx == 1:
            return mock_llm_server._build_completion_payload(
                model=req_json["model"],
                tool_calls=mock_llm_server._make_tool_call_spec(
                    [
                        ("get_log_summary", {"log_id": lid}),
                        ("search_log", {"log_id": lid, "pattern": "TextureCache"}),
                    ]
                )["tool_calls"],
                finish_reason="tool_calls",
            )
        if idx == 2:
            return mock_llm_server._build_completion_payload(
                model=req_json["model"],
                tool_calls=mock_llm_server._make_tool_call_spec(
                    [("search_known_issues", {"query": "SubmitDrawCall"})]
                )["tool_calls"],
                finish_reason="tool_calls",
            )
        verdict = mock_llm_server._make_verdict_spec(
            verdict="NEW_ISSUE",
            suggested_title="ReAct Multi-Turn Null Texture",
            evidence_log_snippets=[
                {"log_id": lid, "start_line": 22, "end_line": 22, "relevance": "null deref"}
            ],
        )
        return mock_llm_server._build_completion_payload(
            model=req_json["model"], content=verdict["content"], finish_reason="stop"
        )

    mock_llm_server.set_handler(_dynamic_handler)
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("app.log", log_bytes)],
    )
    crash_id = up.json()["crash_id"]
    expected_log_id = up.json()["log_ids"][0]
    detail = await helpers.wait_for_crash_status(client, crash_id)

    assert detail["status"] == "ANALYZED"
    run = detail["ai_runs"][0]
    assert run["status"] == "COMPLETED"
    assert run["prompt_tokens"] > 0
    assert run["completion_tokens"] > 0
    assert run["duration_ms"] >= 0
    trace_str = str(run["trace_json"])
    for tool_name in ("get_crash_overview", "get_log_summary", "search_log", "search_known_issues"):
        assert tool_name in trace_str
    third_tool_texts = "\n".join(
        m["content"]
        for m in mock_llm_server.requests[2]["body"]["messages"]
        if m.get("role") == "tool"
    )
    assert "TextureCache::Get" in third_tool_texts
    assert run["result_json"]["evidence_log_snippets"][0]["log_id"] == expected_log_id


async def test_t1_38_ai_known_issue_verdict_links_issue(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="ai-known")
    existing = (
        await client.post(
            "/api/v1/issues",
            json={"project_id": proj["id"], "title": "Existing Known Bug", "status": "KNOWN"},
        )
    ).json()

    mock_llm_server.queue_verdict(
        verdict="KNOWN_ISSUE",
        matched_issue_id=existing["id"],
        confidence=0.94,
    )
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump()
    )
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    assert detail["ai_verdict"] == "KNOWN_ISSUE"
    assert detail["issue"]["id"] == existing["id"]
    assert detail["ai_confidence"] >= 0.9


async def test_t1_39_ai_new_issue_creates_issue(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="ai-new")
    mock_llm_server.queue_verdict(
        verdict="NEW_ISSUE",
        suggested_title="AudioMixer Buffer Underrun",
        root_cause_analysis="Ring buffer underflow on audio thread.",
        suggested_component="Audio",
        key_log_patterns=["AudioMixer.*underrun"],
    )
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump()
    )
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    assert detail["ai_verdict"] == "NEW_ISSUE"
    assert detail["issue"] is not None
    issue_det = (await client.get(f"/api/v1/issues/{detail['issue']['id']}")).json()
    assert issue_det["title"] == "AudioMixer Buffer Underrun"
    assert issue_det["component"] == "Audio"


async def test_t1_40_ai_regression_verdict_detection(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="ai-reg")
    fixed_issue = (
        await client.post(
            "/api/v1/issues",
            json={
                "project_id": proj["id"],
                "title": "Fixed Render Bug",
                "status": "FIXED",
                "fixed_in_version": "1.0.0",
            },
        )
    ).json()
    mock_llm_server.queue_verdict(
        verdict="REGRESSION",
        matched_issue_id=fixed_issue["id"],
        confidence=0.89,
    )
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump(), app_version="1.1.0"
    )
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    assert detail["ai_verdict"] == "REGRESSION"


async def test_t1_41_smart_triage_autoskip_unambiguous_known_cluster(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="ai-smart-skip")
    dmp = fake_toolchain.make_dump()

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Deterministic Known Bug")
    u1 = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)
    c1 = await helpers.wait_for_crash_status(client, u1.json()["crash_id"])
    issue_id = c1["issue"]["id"]

    await client.post(
        f"/api/v1/crashes/{c1['id']}/verdict",
        json={"action": "confirm", "is_ambiguous_stack": False},
    )
    mock_llm_server.reset()

    u2 = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)
    c2 = await helpers.wait_for_crash_status(client, u2.json()["crash_id"])
    assert c2["status"] == "ANALYZED"
    assert c2["issue"]["id"] == issue_id
    assert c2["ai_runs"] == []
    assert len(mock_llm_server.requests) == 0
