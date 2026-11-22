from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.tier4


async def test_t4_01_ci_build_publish_to_customer_crash_and_kb(
    live_server: str,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    import httpx

    async with httpx.AsyncClient(
        base_url=live_server, timeout=30.0, auth=isolated_env["admin_auth"]
    ) as hc:
        proj = (
            await hc.post("/api/v1/projects", json={"key": "studio-game", "name": "Studio Game"})
        ).json()

        build_dir = tmp_path / "studio_build"
        did_game = "410011112222333344445555666677771"
        did_render = "410022223333444455556666777788881"
        fake_toolchain.make_pe("game.exe", "CGAME1", "game.pdb", did_game, write_to=build_dir / "game.exe")
        fake_toolchain.make_pdb("game.pdb", did_game, "game.exe", "CGAME1", write_to=build_dir / "game.pdb")
        fake_toolchain.make_pe("render.dll", "CREND1", "render.pdb", did_render, write_to=build_dir / "render.dll")
        fake_toolchain.make_pdb("render.pdb", did_render, "render.dll", "CREND1", write_to=build_dir / "render.pdb")

        sym_cli = await asyncio.to_thread(
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
        assert sym_cli.returncode == 0, sym_cli.stderr
        syms = (await hc.get("/api/v1/symbols", params={"project_id": proj["id"]})).json()["items"]
        assert len(syms) == 2 and all(s["has_pe_unwind"] is True for s in syms)

        dmp_path = tmp_path / "customer_crash.dmp"
        fake_toolchain.make_dump(
            modules=[
                {"filename": "render.dll", "debug_file": "render.pdb", "debug_id": did_render, "code_id": "CREND1", "always_loaded": False},
                {"filename": "game.exe", "debug_file": "game.pdb", "debug_id": did_game, "code_id": "CGAME1", "always_loaded": False},
            ],
            threads=[
                {
                    "thread_name": "RenderThread",
                    "frames": [
                        {"module": "render.dll", "function": "Renderer::SubmitDrawCall(Mesh*)", "file": "src/render.cpp", "line": 312},
                        {"module": "game.exe", "function": "GameLoop::RenderScene()", "file": "src/game.cpp", "line": 95},
                    ],
                }
            ],
            write_to=dmp_path,
        )

        # ~3MB mixed log with clear root-cause lines
        lines = []
        base_dt = dt.datetime(2026, 11, 22, 10, 0, 0)
        for i in range(1, 25001):
            ts = (base_dt + dt.timedelta(milliseconds=i * 10)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            if i == 24500:
                lines.append(f"{ts} ERROR TextureCache miss fallback null for Mesh_0x42\n")
            elif i == 24501:
                lines.append(f"{ts} FATAL Renderer::SubmitDrawCall failed due to nullptr texture\n")
            else:
                lines.append(f"{ts} INFO Frame tick={i} render_queue_depth=4 ok\n")
        log_path = tmp_path / "customer_3mb.log"
        log_path.write_text("".join(lines), encoding="utf-8")

        def _ai_handler(req_json: dict[str, Any], headers: dict[str, str], idx: int) -> dict[str, Any]:
            if idx == 0:
                return mock_llm_server._build_completion_payload(
                    model=req_json["model"],
                    tool_calls=mock_llm_server._make_tool_call_spec([("get_crash_overview", {})])["tool_calls"],
                    finish_reason="tool_calls",
                )
            tool_msgs = [m["content"] for m in req_json["messages"] if m.get("role") == "tool"]
            lid = helpers.extract_log_id(tool_msgs[0])
            if idx == 1:
                return mock_llm_server._build_completion_payload(
                    model=req_json["model"],
                    tool_calls=mock_llm_server._make_tool_call_spec(
                        [("search_log", {"log_id": lid, "pattern": "TextureCache.*null", "is_regex": True})]
                    )["tool_calls"],
                    finish_reason="tool_calls",
                )
            verdict = mock_llm_server._make_verdict_spec(
                verdict="NEW_ISSUE",
                suggested_title="Renderer::SubmitDrawCall Null Texture",
                root_cause_analysis="TextureCache fallback returned null before SubmitDrawCall.",
                evidence_log_snippets=[{"log_id": lid, "start_line": 24500, "end_line": 24501, "relevance": "Null texture root cause"}],
                key_log_patterns=["TextureCache miss fallback null"],
                suggested_component="Rendering",
            )
            return mock_llm_server._build_completion_payload(
                model=req_json["model"], content=verdict["content"], finish_reason="stop"
            )

        mock_llm_server.set_handler(_ai_handler)
        crash_cli = await asyncio.to_thread(
            helpers.run_cli,
            [
                "upload-crash",
                "--server", live_server,
                "--project", proj["key"],
                "--api-key", proj["api_key"],
                "--dump", str(dmp_path),
                "--log", str(log_path),
                "--app-version", "2.0.0",
                "--wait",
            ],
            isolated_env["proc_env"],
            60,
        )
        assert crash_cli.returncode == 0, crash_cli.stderr

        crashes = (await hc.get("/api/v1/crashes", params={"project_id": proj["id"]})).json()["items"]
        assert len(crashes) == 1
        crash_id = crashes[0]["id"]

        events = await helpers.stream_crash_sse(hc, crash_id)
        assert events[-1][1]["status"] == "ANALYZED"

        detail = (await hc.get(f"/api/v1/crashes/{crash_id}")).json()
        assert detail["missing_symbols_json"] == []
        top_frame = detail["parsed_stacks_json"]["threads"][0]["frames"][0]
        assert top_frame["trust"] == "cfi"

        snip = detail["ai_runs"][0]["result_json"]["evidence_log_snippets"][0]
        lid = detail["logs"][0]["id"]
        assert snip["log_id"] == lid
        ev = (
            await hc.get(f"/api/v1/logs/{snip['log_id']}/lines", params={"start": snip["start_line"], "end": snip["end_line"]})
        ).json()["lines"]
        assert any("TextureCache miss fallback null" in l["text"] for l in ev)

        verdict = await hc.post(
            f"/api/v1/crashes/{crash_id}/verdict",
            json={
                "action": "create_issue",
                "new_issue": {"title": "Confirmed Null Texture Draw", "component": "Rendering"},
                "add_log_patterns": ["SubmitDrawCall failed"],
            },
        )
        assert verdict.status_code == 200
        assert verdict.json()["is_human_verified"] is True

        dash = (await hc.get("/api/v1/dashboard", params={"project_id": proj["id"]})).json()
        assert dash["kpis"]["total_crashes"] == 1
        assert dash["kpis"]["analyzed_crashes"] == 1
        assert dash["kpis"]["open_issues"] >= 1


async def test_t4_02_post_release_regression_incident_workflow(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t4-02-reg")
    dmp = fake_toolchain.make_dump()

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="WorldStreamer Race")
    c1 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp, app_version="1.1.0")).json()["crash_id"],
    )
    issue_id = c1["issue"]["id"]
    await client.patch(
        f"/api/v1/issues/{issue_id}",
        json={"status": "FIXED", "fixed_in_version": "1.2.0", "is_ambiguous_stack": True},
    )

    # Pre-fix build 1.1.5 should not trigger REGRESSION
    mock_llm_server.queue_verdict(verdict="KNOWN_ISSUE", matched_issue_id=issue_id)
    c_pre = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp, app_version="1.1.5")).json()["crash_id"],
    )
    assert c_pre["ai_verdict"] == "KNOWN_ISSUE"
    assert (await client.get(f"/api/v1/issues/{issue_id}")).json()["status"] == "FIXED"

    # Post-fix build 1.2.5 triggers REGRESSION
    mock_llm_server.queue_verdict(verdict="REGRESSION", matched_issue_id=issue_id)
    c_reg = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp, app_version="1.2.5")).json()["crash_id"],
    )
    assert c_reg["ai_verdict"] == "REGRESSION"
    await client.patch(f"/api/v1/crashes/{c_reg['id']}/pin", json={"is_pinned": True})

    issue_after = (await client.get(f"/api/v1/issues/{issue_id}")).json()
    assert issue_after["status"] == "REGRESSION"
    assert issue_after["crash_count"] == 3

    reg_crashes = (await client.get("/api/v1/crashes", params={"project_id": proj["id"], "ai_verdict": "REGRESSION"})).json()
    assert reg_crashes["total"] == 1 and reg_crashes["items"][0]["is_pinned"] is True
    reg_issues = (await client.get("/api/v1/issues", params={"project_id": proj["id"], "status": "REGRESSION"})).json()
    assert reg_issues["total"] == 1

    dash = (await client.get("/api/v1/dashboard", params={"project_id": proj["id"]})).json()
    assert "REGRESSION" in str(dash["verdict_distribution"])
    assert "REGRESSION" in str(dash["issue_status_distribution"])


@pytest.mark.slow
async def test_t4_03_large_mixed_encoding_multichunk_log_workflow(
    client: Any,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t4-03-biglog")

    # > 8.5 MB synthetic mixed UTF-8-BOM + UTF-8 + GB18030 log (>32 chunks at 256KB)
    # with strictly monotonically increasing timestamps
    parts: list[bytes] = [b"\xef\xbb\xbf"]
    base_dt = dt.datetime(2026, 11, 22, 12, 0, 0)
    burst_dt = dt.datetime(2026, 11, 22, 12, 15, 0, 500_000)
    post_burst_dt = dt.datetime(2026, 11, 22, 12, 15, 0, 600_000)

    total_lines = 88_000
    target_burst_line = 70_000
    for i in range(1, total_lines + 1):
        if i < target_burst_line:
            line_dt = base_dt + dt.timedelta(milliseconds=i * 12)
        elif i == target_burst_line:
            line_dt = burst_dt
        else:
            line_dt = post_burst_dt + dt.timedelta(milliseconds=(i - target_burst_line - 1) * 12)
        ts = line_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        if i == target_burst_line:
            parts.append(f"{ts} FATAL BURST_CRASH_TRIGGER TexturePool::Alloc failed\n".encode("utf-8"))
        elif i == target_burst_line + 1:
            parts.append(f"{ts} ERROR 致命渲染崩溃发生 𠮷\n".encode("gb18030"))
        elif i == 1000:
            parts.append(f"{ts} WARN BadSeq ".encode("utf-8") + b"\xff\xfe" + b" recovered\n")
        elif i % 250 == 0:
            parts.append(f"{ts} ERROR 纹理资源池耗尽 TexturePool OOM 𠮷 idx={i}\n".encode("gb18030"))
        else:
            parts.append(
                f"{ts} INFO WorkerLoop step=ok frame_ms=16.2 memory_mb=1024 padding_0123456789abcdef\n".encode("utf-8")
            )
    log_bytes = b"".join(parts)
    assert len(log_bytes) > 8_500_000

    def _ai_handler(req_json: dict[str, Any], headers: dict[str, str], idx: int) -> dict[str, Any]:
        if idx == 0:
            return mock_llm_server._build_completion_payload(
                model=req_json["model"],
                tool_calls=mock_llm_server._make_tool_call_spec([("get_crash_overview", {})])["tool_calls"],
                finish_reason="tool_calls",
            )
        tool_msgs = [m["content"] for m in req_json["messages"] if m.get("role") == "tool"]
        lid = helpers.extract_log_id(tool_msgs[0])
        if idx == 1:
            return mock_llm_server._build_completion_payload(
                model=req_json["model"],
                tool_calls=mock_llm_server._make_tool_call_spec(
                    [("get_log_summary", {"log_id": lid}), ("read_log_around_time", {"log_id": lid, "timestamp": "2026-11-22T12:15:00.500", "window_seconds": 5})]
                )["tool_calls"],
                finish_reason="tool_calls",
            )
        if idx == 2:
            return mock_llm_server._build_completion_payload(
                model=req_json["model"],
                tool_calls=mock_llm_server._make_tool_call_spec(
                    [("search_log", {"log_id": lid, "pattern": "BURST_CRASH_TRIGGER", "is_regex": False})]
                )["tool_calls"],
                finish_reason="tool_calls",
            )
        verdict = mock_llm_server._make_verdict_spec(
            verdict="NEW_ISSUE",
            suggested_title="TexturePool OOM Crash",
            evidence_log_snippets=[{"log_id": lid, "start_line": target_burst_line, "end_line": target_burst_line + 1, "relevance": "Burst OOM"}],
        )
        return mock_llm_server._build_completion_payload(
            model=req_json["model"], content=verdict["content"], finish_reason="stop"
        )

    mock_llm_server.set_handler(_ai_handler)
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("huge.log", log_bytes)],
        crash_time="2026-11-22T12:15:01Z",
    )
    crash_id = up.json()["crash_id"]
    log_id = up.json()["log_ids"][0]
    detail = await helpers.wait_for_crash_status(client, crash_id, timeout=30.0)
    assert detail["status"] == "ANALYZED"

    # Verify chunked .log.zst and .idx.json on disk
    idx_files = list((isolated_env["data_dir"] / "logs").rglob("*.idx.json"))
    zst_files = list((isolated_env["data_dir"] / "logs").rglob("*.log.zst"))
    assert len(idx_files) >= 1 and len(zst_files) >= 1
    idx_data = json.loads(idx_files[0].read_text(encoding="utf-8"))
    assert len(idx_data["chunks"]) > 32
    for ch in idx_data["chunks"][:-1]:
        assert 200_000 <= ch["uncompressed_size"] <= 280_000

    summary = (await client.get(f"/api/v1/logs/{log_id}/summary")).json()
    assert summary["total_lines"] == total_lines
    assert summary["raw_size"] / summary["compressed_size"] >= 8.0

    # Read across chunk boundary
    c0_end = idx_data["chunks"][0]["end_line"]
    cross = (await client.get(f"/api/v1/logs/{log_id}/lines", params={"start": c0_end - 5, "end": c0_end + 5})).json()
    assert [l["line_number"] for l in cross["lines"]] == list(range(c0_end - 5, c0_end + 6))

    around = (
        await client.get(f"/api/v1/logs/{log_id}/around-time", params={"timestamp": "2026-11-22T12:15:00.500", "window_sec": 3})
    ).json()
    around_texts = [l["text"] for l in around["lines"]]
    assert any("BURST_CRASH_TRIGGER" in txt for txt in around_texts)
    assert any("致命渲染崩溃发生 𠮷" in txt for txt in around_texts)

    search = (
        await client.get(f"/api/v1/logs/{log_id}/search", params={"pattern": "BURST_CRASH_TRIGGER.*Alloc", "is_regex": "true"})
    ).json()
    assert search["total_matches"] == 1
    assert search["elapsed_ms"] < 500
    assert search["matches"][0]["line_number"] == target_burst_line

    gb18030_search = (
        await client.get(f"/api/v1/logs/{log_id}/search", params={"pattern": "致命渲染崩溃发生 𠮷", "is_regex": "false"})
    ).json()
    assert gb18030_search["total_matches"] == 1
    assert gb18030_search["matches"][0]["line_number"] == target_burst_line + 1

    assert detail["ai_runs"][0]["result_json"]["evidence_log_snippets"][0]["log_id"] == log_id
    trace_s = str(detail["ai_runs"][0]["trace_json"])
    assert "get_log_summary" in trace_s and "read_log_around_time" in trace_s and "search_log" in trace_s


async def test_t4_04_missing_symbol_backlog_late_upload_resymbolicate_recluster(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t4-04-backlog")
    did_1 = "DEAD00000000000000000000000000011"
    did_2 = "DEAD00000000000000000000000000021"

    dmp_1 = fake_toolchain.make_dump(
        modules=[{"filename": "Physics.dll", "debug_file": "Physics.pdb", "debug_id": did_1, "code_id": "CPHY1", "always_loaded": False}],
        threads=[{"thread_name": "M", "frames": [{"module": "Physics.dll", "function": "Physics::StepWorld(float)", "file": "src/phys.cpp", "line": 44}]}],
    )
    dmp_2 = fake_toolchain.make_dump(
        modules=[{"filename": "Audio.dll", "debug_file": "Audio.pdb", "debug_id": did_2, "code_id": "CAUD2", "always_loaded": False}],
        threads=[{"thread_name": "M", "frames": [{"module": "Audio.dll", "function": "Audio::Mix()", "file": "src/aud.cpp", "line": 12}]}],
    )

    c1_ids = []
    for i in range(3):
        mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title=f"Unresolved Phys {i}")
        up = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp_1)
        c1_ids.append(up.json()["crash_id"])
        await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Unresolved Audio")
    up_2 = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp_2)
    await helpers.wait_for_crash_status(client, up_2.json()["crash_id"])

    miss = (await client.get("/api/v1/symbols/missing", params={"project_id": proj["id"]})).json()
    assert miss[0]["debug_id"] == did_1 and miss[0]["crash_count"] == 3
    assert len(miss[0]["affected_crash_ids"]) == 3
    assert miss[1]["debug_id"] == did_2 and miss[1]["crash_count"] == 1

    # Upload PDB-only first, verify has_pe_unwind=False
    pdb_1 = fake_toolchain.make_pdb("Physics.pdb", did_1, "Physics.dll", "CPHY1")
    r_pdb_only = await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb_1, "Physics.pdb")
    assert r_pdb_only.status_code == 201 and r_pdb_only.json()["has_pe_unwind"] is False

    # Upload full PDB+PE and resymbolicate
    pe_1 = fake_toolchain.make_pe("Physics.dll", "CPHY1", "Physics.pdb", did_1)
    r_full = await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb_1, "Physics.pdb", pe_1, "Physics.dll")
    assert r_full.status_code == 201
    assert r_full.json()["has_pe_unwind"] is True

    for _ in range(3):
        mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Physics::StepWorld Crash")
    resym = await client.post("/api/v1/symbols/resymbolicate", json={"project_id": proj["id"], "debug_id": did_1})
    assert resym.status_code == 202
    assert resym.json()["queued_count"] == 3

    resolved_details = []
    for cid in c1_ids:
        for _ in range(150):
            d = (await client.get(f"/api/v1/crashes/{cid}")).json()
            if d["missing_symbols_json"] == [] and d["status"] == "ANALYZED":
                resolved_details.append(d)
                break
            await asyncio.sleep(0.1)

    assert len(resolved_details) == 3
    assert len({d["cluster"]["id"] for d in resolved_details}) == 1
    top_frame = resolved_details[0]["parsed_stacks_json"]["threads"][0]["frames"][0]
    assert top_frame["function"] == "Physics::StepWorld(float)"
    assert top_frame["trust"] == "cfi"

    miss_after = (await client.get("/api/v1/symbols/missing", params={"project_id": proj["id"]})).json()
    assert not any(m["debug_id"] == did_1 for m in miss_after)
    assert any(m["debug_id"] == did_2 for m in miss_after)


async def test_t4_05_disk_exhaustion_safety_floor_and_pinned_protection(
    client: Any,
    fake_toolchain: Any,
    fake_disk_usage: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    await client.put("/api/v1/settings", json={"archive_raw_symbols": True, "host_safety_floor_gb": 50.0})
    p1 = await helpers.create_project(client, key="t4-05-p1")
    p2 = await helpers.create_project(client, key="t4-05-p2")

    crash_ids: list[tuple[str, str]] = []
    for idx in range(5):
        proj = p1 if idx < 3 else p2
        mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title=f"Disk Crash {idx}")
        up = await helpers.upload_crash(
            client,
            proj["key"],
            proj["api_key"],
            fake_toolchain.make_dump(
                extra_bytes=f"DMP_{idx}".encode("utf-8"),
                threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": f"DiskFn{idx}()"}]}],
            ),
            log_files=[(f"d{idx}.log", helpers.make_mixed_encoding_log(num_lines=25))],
        )
        cid = up.json()["crash_id"]
        lid = up.json()["log_ids"][0]
        await helpers.wait_for_crash_status(client, cid)
        crash_ids.append((cid, lid))

    c1_id, c1_log = crash_ids[0]
    c2_id, c2_log = crash_ids[1]
    await client.patch(f"/api/v1/crashes/{c1_id}/pin", json={"is_pinned": True})
    c2_det = (await client.get(f"/api/v1/crashes/{c2_id}")).json()
    await client.patch(f"/api/v1/issues/{c2_det['issue']['id']}", json={"is_pinned": True})

    fake_disk_usage.set_free_gb(42.0)
    dash = (await client.get("/api/v1/dashboard")).json()
    assert dash["storage"]["safety_floor_breached"] is True

    sym = await helpers.upload_symbol(
        client,
        p1["key"],
        p1["api_key"],
        fake_toolchain.make_pdb("FloorSkip.pdb", "450011112222333344445555666677771"),
        "FloorSkip.pdb",
    )
    assert sym.status_code == 201 and sym.json()["raw_size"] == 0

    gc1 = (await client.post("/api/v1/settings/gc", json={"force_emergency": True})).json()
    assert gc1["emergency_triggered"] is True
    assert gc1["deleted_files_count"] > 0
    assert gc1["skipped_pinned_count"] >= 2

    # Pinned C1 & C2 survive
    for cid, lid in (crash_ids[0], crash_ids[1]):
        assert (await client.get(f"/api/v1/crashes/{cid}/artifacts/dump")).status_code == 200
        assert (await client.get(f"/api/v1/logs/{lid}/lines", params={"start": 1, "end": 5})).status_code == 200

    # Unpinned C3-C5 evicted on disk, DB intact
    for cid, lid in crash_ids[2:]:
        assert (await client.get(f"/api/v1/crashes/{cid}/artifacts/dump")).status_code in (404, 410)
        assert (await client.get(f"/api/v1/logs/{lid}/lines", params={"start": 1, "end": 5})).status_code in (404, 410)
        db_row = await client.get(f"/api/v1/crashes/{cid}")
        assert db_row.status_code == 200 and db_row.json()["parsed_stacks_json"] is not None

    # Idempotent repeated GC
    gc2 = await client.post("/api/v1/settings/gc", json={"force_emergency": True})
    assert gc2.status_code == 200


async def test_t4_06_ambiguous_stack_disambiguation_via_logs_and_ai(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t4-06-ambig")
    issue_a = (
        await client.post(
            "/api/v1/issues",
            json={
                "project_id": proj["id"],
                "title": "VRAM Exhausted",
                "status": "KNOWN",
                "is_ambiguous_stack": True,
                "log_patterns_json": ["D3D11_ERROR_OUT_OF_MEMORY"],
            },
        )
    ).json()
    issue_b = (
        await client.post(
            "/api/v1/issues",
            json={
                "project_id": proj["id"],
                "title": "Shader Compile Failed",
                "status": "KNOWN",
                "is_ambiguous_stack": True,
                "log_patterns_json": ["FXC compile error"],
            },
        )
    ).json()

    shared_dmp = fake_toolchain.make_dump(
        threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": "Engine::FatalErrorAssert()"}]}]
    )

    def _disambig_handler(req_json: dict[str, Any], headers: dict[str, str], idx: int) -> dict[str, Any]:
        msgs = req_json["messages"]
        tc_names: dict[str, str] = {}
        for m in msgs:
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    tc_id = tc.get("id")
                    fn_name = (tc.get("function") or {}).get("name")
                    if tc_id and fn_name:
                        tc_names[tc_id] = fn_name

        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        called_tools = set(tc_names.values())
        if "get_crash_overview" not in called_tools:
            return mock_llm_server._build_completion_payload(
                model=req_json["model"],
                tool_calls=mock_llm_server._make_tool_call_spec([("get_crash_overview", {})])["tool_calls"],
                finish_reason="tool_calls",
            )
        if "get_log_summary" not in called_tools:
            lid = helpers.extract_log_id(tool_msgs[0]["content"])
            return mock_llm_server._build_completion_payload(
                model=req_json["model"],
                tool_calls=mock_llm_server._make_tool_call_spec(
                    [
                        ("get_log_summary", {"log_id": lid}),
                        ("search_log", {"log_id": lid, "pattern": "ERROR"}),
                        ("search_known_issues", {"query": "FatalErrorAssert"}),
                        ("get_issue_detail", {"issue_id": issue_a["id"]}),
                    ]
                )["tool_calls"],
                finish_reason="tool_calls",
            )
        log_tool_outputs = [
            m["content"]
            for m in tool_msgs
            if tc_names.get(m.get("tool_call_id", "")) in ("get_log_summary", "search_log")
        ]
        log_text = "\n".join(log_tool_outputs)
        if "FXC compile error" in log_text:
            matched = issue_b["id"]
        elif "D3D11_ERROR_OUT_OF_MEMORY" in log_text:
            matched = issue_a["id"]
        else:
            raise AssertionError(f"Expected disambiguation pattern in log tool outputs, got: {log_tool_outputs}")
        verdict = mock_llm_server._make_verdict_spec(verdict="KNOWN_ISSUE", matched_issue_id=matched)
        return mock_llm_server._build_completion_payload(
            model=req_json["model"], content=verdict["content"], finish_reason="stop"
        )

    mock_llm_server.set_handler(_disambig_handler)
    c1 = await helpers.wait_for_crash_status(
        client,
        (
            await helpers.upload_crash(
                client,
                proj["key"],
                proj["api_key"],
                shared_dmp,
                log_files=[("vram.log", b"2026-11-22 10:00:00.000 ERROR D3D11_ERROR_OUT_OF_MEMORY\n")],
            )
        ).json()["crash_id"],
    )
    assert c1["issue"]["id"] == issue_a["id"]
    # Confirm cluster link & keep ambiguous flag
    await client.post(
        f"/api/v1/crashes/{c1['id']}/verdict",
        json={"action": "assign_issue", "issue_id": issue_a["id"], "is_ambiguous_stack": True},
    )

    c2 = await helpers.wait_for_crash_status(
        client,
        (
            await helpers.upload_crash(
                client,
                proj["key"],
                proj["api_key"],
                shared_dmp,
                log_files=[("shader.log", b"2026-11-22 10:00:00.000 ERROR FXC compile error PS_Main\n")],
            )
        ).json()["crash_id"],
    )
    assert len(c2["ai_runs"]) >= 1
    assert c2["issue"]["id"] == issue_b["id"]
    trace_s = str(c2["ai_runs"][0]["trace_json"])
    for tname in ("get_crash_overview", "get_log_summary", "search_log", "search_known_issues", "get_issue_detail"):
        assert tname in trace_s


async def test_t4_07_issue_triage_housekeeping_merge_split_reprocess(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t4-07-housekeep")
    crashes = []
    for idx, fn in enumerate(["RenderA()", "RenderB()", "AudioC()"]):
        mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title=f"Title {fn}", key_log_patterns=[f"PAT_{idx}"])
        d = fake_toolchain.make_dump(threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": fn}]}])
        up = await helpers.upload_crash(client, proj["key"], proj["api_key"], d)
        crashes.append(await helpers.wait_for_crash_status(client, up.json()["crash_id"]))

    i1, i2, i3 = [c["issue"]["id"] for c in crashes]
    merged = (
        await client.post("/api/v1/issues/merge", json={"target_issue_id": i1, "source_issue_ids": [i2, i3]})
    ).json()
    assert merged["crash_count"] == 3
    assert len(merged["clusters"]) == 3
    assert (await client.get(f"/api/v1/issues/{i2}")).status_code == 404
    assert (await client.get(f"/api/v1/issues/{i3}")).status_code == 404

    cl_c = crashes[2]["cluster"]["id"]
    split = (
        await client.post(f"/api/v1/issues/{i1}/split", json={"cluster_id": cl_c, "new_issue_title": "Audio Deadlock"})
    ).json()
    i4 = split["target_issue"]["id"]
    assert split["source_issue"]["crash_count"] == 2
    assert split["target_issue"]["crash_count"] == 1
    assert split["target_issue"]["title"] == "Audio Deadlock"

    # Splitting same cluster again from i1 must fail 400
    assert (
        await client.post(f"/api/v1/issues/{i1}/split", json={"cluster_id": cl_c, "new_issue_title": "Dup"})
    ).status_code == 400

    mock_llm_server.queue_verdict(verdict="KNOWN_ISSUE", matched_issue_id=i4)
    reproc = await client.post(f"/api/v1/crashes/{crashes[2]['id']}/reprocess", json={"mode": "triage"})
    assert reproc.status_code == 202
    cur: dict[str, Any] = {}
    for _ in range(300):
        cur = (await client.get(f"/api/v1/crashes/{crashes[2]['id']}")).json()
        if len(cur["ai_runs"]) >= 2 and cur["status"] == "ANALYZED":
            break
        await asyncio.sleep(0.05)
    assert cur["status"] == "ANALYZED"
    assert len(cur["ai_runs"]) >= 2
    assert cur["ai_runs"][-1]["status"] == "COMPLETED"
    assert cur["issue"]["id"] == i4

    dash = (await client.get("/api/v1/dashboard", params={"project_id": proj["id"]})).json()
    assert dash["kpis"]["total_crashes"] == 3
    assert dash["kpis"]["open_issues"] == 2


async def test_t4_08_multiproject_isolation_and_key_compromise_recovery(
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
        p_win = (await hc.post("/api/v1/projects", json={"key": "game-win", "name": "Game Win"})).json()
        p_mac = (await hc.post("/api/v1/projects", json={"key": "editor-mac", "name": "Editor Mac"})).json()

        dmp_path = tmp_path / "iso.dmp"
        dmp_bytes = fake_toolchain.make_dump()
        dmp_path.write_bytes(dmp_bytes)

        mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Win Bug")
        c_win = (
            await helpers.upload_crash(hc, p_win["key"], p_win["api_key"], dmp_bytes)
        ).json()["crash_id"]
        d_win = await helpers.wait_for_crash_status(hc, c_win)

        mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Mac Bug")
        c_mac = (
            await helpers.upload_crash(hc, p_mac["key"], p_mac["api_key"], dmp_bytes)
        ).json()["crash_id"]
        d_mac = await helpers.wait_for_crash_status(hc, c_mac)

        assert (await helpers.upload_crash(hc, p_mac["key"], p_win["api_key"], dmp_bytes)).status_code == 403
        assert (
            await hc.post("/api/v1/issues/merge", json={"target_issue_id": d_win["issue"]["id"], "source_issue_ids": [d_mac["issue"]["id"]]})
        ).status_code == 400

        old_win_key = p_win["api_key"]
        new_win_key = (await hc.post(f"/api/v1/projects/{p_win['id']}/rotate-key")).json()["api_key"]

        # Old key rejected via header & form field & CLI
        assert (await helpers.upload_crash(hc, p_win["key"], old_win_key, dmp_bytes, use_header=True)).status_code == 403
        assert (await helpers.upload_crash(hc, p_win["key"], old_win_key, dmp_bytes, use_header=False)).status_code == 403

        cli_old = await asyncio.to_thread(
            helpers.run_cli,
            [
                "upload-crash", "--server", live_server, "--project", p_win["key"],
                "--api-key", old_win_key, "--dump", str(dmp_path), "--app-version", "1.0.0",
            ],
            isolated_env["proc_env"],
        )
        assert cli_old.returncode != 0

        # editor-mac key unaffected
        mock_llm_server.queue_verdict()
        assert (await helpers.upload_crash(hc, p_mac["key"], p_mac["api_key"], dmp_bytes)).status_code == 202

        # new game-win key succeeds via HTTP and CLI
        mock_llm_server.queue_verdict()
        cli_new = await asyncio.to_thread(
            helpers.run_cli,
            [
                "upload-crash", "--server", live_server, "--project", p_win["key"],
                "--api-key", new_win_key, "--dump", str(dmp_path), "--app-version", "1.0.1", "--wait",
            ],
            isolated_env["proc_env"],
        )
        assert cli_new.returncode == 0


async def test_t4_09_ai_failure_degradation_and_recovery_workflow(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    await client.put("/api/v1/settings", json={"ai_max_steps": 2, "ai_timeout_seconds": 2, "llm_api_key": "sk-secret-9999"})
    s_get = (await client.get("/api/v1/settings")).json()
    assert s_get["llm_api_key_set"] is True
    assert "sk-secret-9999" not in str(s_get["llm_api_key_masked"])

    proj = await helpers.create_project(client, key="t4-09-recover")
    mock_llm_server.set_loop_forever("get_crash_overview", {})
    c1_id = (
        await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump())
    ).json()["crash_id"]
    c1 = await helpers.wait_for_crash_status(client, c1_id)
    assert c1["status"] == "ANALYZED"
    assert c1["ai_verdict"] == "NEEDS_REVIEW"
    assert c1["parsed_stacks_json"] is not None

    # Recover with working LLM and reprocess triage
    mock_llm_server.reset()
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Recovered Crash")
    await client.put("/api/v1/settings", json={"ai_max_steps": 10, "ai_timeout_seconds": 30})
    rep = await client.post(f"/api/v1/crashes/{c1_id}/reprocess", json={"mode": "triage"})
    assert rep.status_code == 202

    events = await helpers.stream_crash_sse(client, c1_id)
    assert events[-1][1]["status"] == "ANALYZED"

    after = (await client.get(f"/api/v1/crashes/{c1_id}")).json()
    assert len(after["ai_runs"]) == 2
    assert after["ai_runs"][-1]["status"] == "COMPLETED"
    assert after["ai_verdict"] == "NEW_ISSUE"


async def test_t4_10_windbg_symsrv_and_artifact_export_workflow(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    await client.put("/api/v1/settings", json={"archive_raw_symbols": True})
    proj = await helpers.create_project(client, key="t4-10-symsrv")

    did = "4A0011112222333344445555666677771"
    cid = "65004A001122"
    pdb_bytes = fake_toolchain.make_pdb("engine.pdb", did, "engine.exe", cid, extra_bytes=b"EXACT_PDB_BYTES_999")
    pe_bytes = fake_toolchain.make_pe("engine.exe", cid, "engine.pdb", did, extra_bytes=b"EXACT_PE_BYTES_888")

    up_sym = await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb_bytes, "engine.pdb", pe_bytes, "engine.exe")
    assert up_sym.status_code == 201

    dmp_bytes = fake_toolchain.make_dump(
        modules=[{"filename": "engine.exe", "debug_file": "engine.pdb", "debug_id": did, "code_id": cid, "always_loaded": False}],
        extra_bytes=b"EXACT_DUMP_BINARY_BYTES_777",
    )
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Engine Artifact Crash")
    up_crash = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp_bytes)
    crash_id = up_crash.json()["crash_id"]
    detail = await helpers.wait_for_crash_status(client, crash_id)

    assert (await client.get("/symsrv/pingme.txt")).status_code == 200
    assert (await client.get("/symsrv/index2.txt")).status_code == 404
    assert (await client.head(f"/symsrv/engine.pdb/{did}/engine.pdb")).status_code == 200
    assert (await client.get(f"/symsrv/engine.pdb/{did}/engine.pdb")).content == pdb_bytes
    assert (await client.get(f"/symsrv/engine.exe/{cid}/engine.exe")).content == pe_bytes
    assert (await client.get("/symsrv/engine.pdb/MISSING_ID/engine.pdb")).status_code == 404

    dump_art = await client.get(f"/api/v1/crashes/{crash_id}/artifacts/dump")
    assert dump_art.status_code == 200
    assert "attachment" in dump_art.headers.get("content-disposition", "").lower()
    assert dump_art.content == dmp_bytes

    stacks_art = await client.get(f"/api/v1/crashes/{crash_id}/artifacts/stacks_json")
    assert stacks_art.status_code == 200
    assert stacks_art.json() == detail["parsed_stacks_json"]

    assert (await client.get(f"/api/v1/crashes/{crash_id}/artifacts/unknown_kind")).status_code in (400, 422)
