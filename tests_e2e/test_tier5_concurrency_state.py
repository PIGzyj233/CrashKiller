from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytestmark = pytest.mark.tier5


async def test_t5_01_concurrent_symbol_upload_dedup_race(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    """Simultaneous identical symbol uploads must never 500; all return 201 sharing 1 row."""
    proj = await helpers.create_project(client, key="t5-conc-sym")
    pdb = fake_toolchain.make_pdb(
        filename="SharedCore.pdb",
        debug_id="510011112222333344445555666677771",
        code_file="SharedCore.dll",
        code_id="65001111",
    )

    responses = await asyncio.gather(
        *[
            helpers.upload_symbol(
                client,
                proj["key"],
                proj["api_key"],
                pdb,
                pdb_filename="SharedCore.pdb",
                app_version="1.0.0",
            )
            for _ in range(4)
        ]
    )
    for resp in responses:
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"

    bodies = [r.json() for r in responses]
    unique_ids = {b["id"] for b in bodies}
    assert len(unique_ids) == 1
    assert any(b["deduplicated"] is False for b in bodies)
    assert sum(1 for b in bodies if b["deduplicated"] is False) == 1
    assert sum(1 for b in bodies if b["deduplicated"] is True) == 3

    listed = (await client.get("/api/v1/symbols", params={"project_id": proj["id"]})).json()
    assert listed["total"] == 1


async def test_t5_02_cross_project_symbol_pe_unwind_isolation(
    client: Any,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    """Project B uploading PDB-only must not strip STACK CFI from Project A's symbols."""
    proj_a = await helpers.create_project(
        client,
        key="t5-iso-a",
        settings_json={"ai_triage_mode": "MANUAL"},
    )
    proj_b = await helpers.create_project(
        client,
        key="t5-iso-b",
        settings_json={"ai_triage_mode": "MANUAL"},
    )

    did = "520011112222333344445555666677771"
    pdb = fake_toolchain.make_pdb(
        filename="EngineCore.pdb",
        debug_id=did,
        code_file="EngineCore.dll",
        code_id="65002222",
    )
    pe = fake_toolchain.make_pe(
        filename="EngineCore.dll",
        code_id="65002222",
        debug_file="EngineCore.pdb",
        debug_id=did,
    )

    up_a = await helpers.upload_symbol(
        client,
        proj_a["key"],
        proj_a["api_key"],
        pdb,
        pdb_filename="EngineCore.pdb",
        pe_bytes=pe,
        pe_filename="EngineCore.dll",
    )
    assert up_a.status_code == 201
    sym_a = up_a.json()
    assert sym_a["has_pe_unwind"] is True

    # Project B uploads PDB-only with the exact same (debug_file, debug_id)
    up_b = await helpers.upload_symbol(
        client,
        proj_b["key"],
        proj_b["api_key"],
        pdb,
        pdb_filename="EngineCore.pdb",
        pe_bytes=None,
    )
    assert up_b.status_code == 201
    sym_b = up_b.json()
    assert sym_b["has_pe_unwind"] is False

    # Verify Project A's .sym on disk retains STACK CFI and Project B's does not
    sym_a_path = isolated_env["data_dir"] / sym_a["sym_path"]
    sym_b_path = isolated_env["data_dir"] / sym_b["sym_path"]
    assert sym_a_path != sym_b_path
    assert "STACK CFI" in sym_a_path.read_text(encoding="utf-8")
    assert "STACK CFI" not in sym_b_path.read_text(encoding="utf-8")

    dmp = fake_toolchain.make_dump(
        modules=[
            {
                "filename": "EngineCore.dll",
                "debug_file": "EngineCore.pdb",
                "debug_id": did,
                "code_file": "EngineCore.dll",
                "code_id": "65002222",
            }
        ],
        threads=[
            {
                "thread_name": "Main",
                "frames": [{"module": "EngineCore.dll", "function": "Engine::Step()"}],
            }
        ],
    )

    c_a_up = await helpers.upload_crash(client, proj_a["key"], proj_a["api_key"], dmp)
    c_b_up = await helpers.upload_crash(client, proj_b["key"], proj_b["api_key"], dmp)
    c_a = await helpers.wait_for_crash_status(client, c_a_up.json()["crash_id"])
    c_b = await helpers.wait_for_crash_status(client, c_b_up.json()["crash_id"])

    assert c_a["parsed_stacks_json"]["threads"][0]["frames"][0]["trust"] == "cfi"
    assert c_b["parsed_stacks_json"]["threads"][0]["frames"][0]["trust"] != "cfi"

    # Deleting Project B's symbol must not delete Project A's .sym
    del_b = await client.delete(f"/api/v1/symbols/{sym_b['id']}")
    assert del_b.status_code == 204
    assert sym_a_path.is_file()
    assert not sym_b_path.exists()

    re_a = await client.post(f"/api/v1/crashes/{c_a['id']}/reprocess", json={"mode": "full"})
    assert re_a.status_code == 202
    c_a_after = await helpers.wait_for_crash_status(client, c_a["id"])
    assert c_a_after["parsed_stacks_json"]["threads"][0]["frames"][0]["trust"] == "cfi"


async def test_t5_03_concurrent_crash_ingestion_cluster_and_issue_counts(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    """Concurrent crash uploads into the same cluster/issue must preserve exact crash_count."""
    proj = await helpers.create_project(
        client,
        key="t5-conc-crash",
        settings_json={"ai_triage_mode": "SMART_TRIAGE"},
    )
    dmp = fake_toolchain.make_dump(
        threads=[
            {
                "thread_name": "Main",
                "frames": [{"module": "App.exe", "function": "ConcurrentRace::Trigger()"}],
            }
        ]
    )

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Concurrent Race Issue")
    seed_up = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)
    assert seed_up.status_code == 202
    seed_detail = await helpers.wait_for_crash_status(client, seed_up.json()["crash_id"])
    assert seed_detail["status"] == "ANALYZED"
    issue_id = seed_detail["issue"]["id"]
    cluster_id = seed_detail["cluster"]["id"]

    # Fire 8 concurrent crash uploads that hit the existing cluster + issue
    uploads = await asyncio.gather(
        *[helpers.upload_crash(client, proj["key"], proj["api_key"], dmp) for _ in range(8)]
    )
    for up in uploads:
        assert up.status_code == 202

    results = await asyncio.gather(
        *[helpers.wait_for_crash_status(client, up.json()["crash_id"]) for up in uploads]
    )
    assert all(r["status"] == "ANALYZED" for r in results)

    issue_detail = (await client.get(f"/api/v1/issues/{issue_id}")).json()
    assert issue_detail["crash_count"] == 9
    clusters = (
        await client.get("/api/v1/issues/clusters", params={"project_id": proj["id"]})
    ).json()["items"]
    matched_cluster = next(c for c in clusters if c["id"] == cluster_id)
    assert matched_cluster["crash_count"] == 9


async def test_t5_06_concurrent_reprocess_409_and_crash_count_integrity(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    """In-flight reprocess must return 409 Conflict and never double-increment Issue.crash_count."""
    proj = await helpers.create_project(
        client,
        key="t5-reproc-409",
        settings_json={"ai_triage_mode": "ALWAYS"},
    )
    dmp = fake_toolchain.make_dump()
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Single Crash Issue")
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)
    cid = up.json()["crash_id"]
    detail = await helpers.wait_for_crash_status(client, cid)
    issue_id = detail["issue"]["id"]

    # Hold LLM response so reprocess stays in TRIAGING
    mock_llm_server.set_delay(0.8)
    mock_llm_server.queue_verdict(
        verdict="KNOWN_ISSUE",
        matched_issue_id=issue_id,
        suggested_title="Single Crash Issue",
    )

    r1 = await client.post(f"/api/v1/crashes/{cid}/reprocess", json={"mode": "triage"})
    assert r1.status_code == 202

    # Subsequent reprocess attempts while TRIAGING/PENDING must return 409
    r2 = await client.post(f"/api/v1/crashes/{cid}/reprocess", json={"mode": "triage"})
    r3 = await client.post(f"/api/v1/crashes/{cid}/reprocess", json={"mode": "full"})
    assert r2.status_code == 409
    assert r3.status_code == 409

    mock_llm_server.set_delay(0.0)
    final_detail = await helpers.wait_for_crash_status(client, cid)
    assert final_detail["status"] == "ANALYZED"
    assert (await client.get(f"/api/v1/issues/{issue_id}")).json()["crash_count"] == 1


async def test_t5_07_split_cluster_preserves_individually_reassigned_crashes(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    """POST /issues/{id}/split must not steal crashes reassigned to a third issue."""
    proj = await helpers.create_project(
        client,
        key="t5-split-preserve",
        settings_json={"ai_triage_mode": "SMART_TRIAGE"},
    )

    issue_c = (
        await client.post(
            "/api/v1/issues",
            json={
                "project_id": proj["id"],
                "title": "Third Ambiguous Issue C",
                "status": "KNOWN",
                "is_ambiguous_stack": True,
            },
        )
    ).json()

    dmp = fake_toolchain.make_dump(
        threads=[
            {
                "thread_name": "Main",
                "frames": [{"module": "App.exe", "function": "SharedCrash()"}],
            }
        ]
    )
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Issue A")
    up1 = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)
    c1 = await helpers.wait_for_crash_status(client, up1.json()["crash_id"])
    issue_a_id = c1["issue"]["id"]
    cluster_id = c1["cluster"]["id"]

    up2 = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)
    c2 = await helpers.wait_for_crash_status(client, up2.json()["crash_id"])
    assert c2["cluster"]["id"] == cluster_id

    # Reassign c1 individually to Issue C (ambiguous), then re-link cluster via c2 to Issue A
    v1 = await client.post(
        f"/api/v1/crashes/{c1['id']}/verdict",
        json={"action": "assign_issue", "issue_id": issue_c["id"], "is_ambiguous_stack": True},
    )
    assert v1.status_code == 200
    v2 = await client.post(
        f"/api/v1/crashes/{c2['id']}/verdict",
        json={"action": "assign_issue", "issue_id": issue_a_id, "is_ambiguous_stack": False},
    )
    assert v2.status_code == 200

    # Split cluster from Issue A into new Issue D
    split_resp = await client.post(
        f"/api/v1/issues/{issue_a_id}/split",
        json={"cluster_id": cluster_id, "new_issue_title": "Split Issue D"},
    )
    assert split_resp.status_code == 200
    split_body = split_resp.json()
    issue_d_id = split_body["target_issue"]["id"]
    assert split_body["source_issue"]["crash_count"] == 0
    assert split_body["target_issue"]["crash_count"] == 1

    c1_after = (await client.get(f"/api/v1/crashes/{c1['id']}")).json()
    c2_after = (await client.get(f"/api/v1/crashes/{c2['id']}")).json()
    assert c1_after["issue_id"] == issue_c["id"]
    assert c2_after["issue_id"] == issue_d_id
    assert (await client.get(f"/api/v1/issues/{issue_c['id']}")).json()["crash_count"] == 1


async def test_t5_08_merge_issues_preserves_pinned_gc_protection(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    """Merging a pinned source issue into an unpinned target must propagate is_pinned=True."""
    proj = await helpers.create_project(client, key="t5-merge-pin")
    target_issue = (
        await client.post(
            "/api/v1/issues",
            json={
                "project_id": proj["id"],
                "title": "Unpinned Target",
                "status": "KNOWN",
                "is_pinned": False,
            },
        )
    ).json()

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Pinned Source Issue")
    dmp = fake_toolchain.make_dump()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        dmp,
        log_files=[("pinned.log", b"2026-11-22 10:00:00.000 ERROR keep me\n")],
    )
    cid = up.json()["crash_id"]
    lid = up.json()["log_ids"][0]
    detail = await helpers.wait_for_crash_status(client, cid)
    src_issue_id = detail["issue"]["id"]

    pin_patch = await client.patch(f"/api/v1/issues/{src_issue_id}", json={"is_pinned": True})
    assert pin_patch.status_code == 200
    assert pin_patch.json()["is_pinned"] is True

    merged = await client.post(
        "/api/v1/issues/merge",
        json={"target_issue_id": target_issue["id"], "source_issue_ids": [src_issue_id]},
    )
    assert merged.status_code == 200
    assert merged.json()["is_pinned"] is True

    gc = (await client.post("/api/v1/settings/gc", json={"force_emergency": True})).json()
    assert gc["skipped_pinned_count"] >= 1
    assert (await client.get(f"/api/v1/crashes/{cid}/artifacts/dump")).status_code == 200
    assert (await client.get(f"/api/v1/logs/{lid}/lines")).status_code == 200


async def test_t5_09_resymbolicate_manual_mode_clears_stale_verdict_and_reconciles_counts(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    """Re-clustering into a new cluster in MANUAL mode clears stale verdict and updates counts."""
    proj = await helpers.create_project(
        client,
        key="t5-resym-manual",
        settings_json={"ai_triage_mode": "SMART_TRIAGE"},
    )
    did = "590011112222333344445555666677771"
    dmp = fake_toolchain.make_dump(
        modules=[
            {
                "filename": "LateMod.dll",
                "debug_file": "LateMod.pdb",
                "debug_id": did,
                "code_file": "LateMod.dll",
                "code_id": "65009999",
            }
        ],
        threads=[
            {
                "thread_name": "Main",
                "frames": [{"module": "LateMod.dll", "function": "LateMod::ResolvedFn()"}],
            }
        ],
    )
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Unresolved Crash Issue")
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)
    c_before = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    assert c_before["ai_verdict"] == "NEW_ISSUE"
    assert len(c_before["missing_symbols_json"]) == 1
    old_issue_id = c_before["issue"]["id"]
    old_cluster_id = c_before["cluster"]["id"]

    # Switch to MANUAL mode and upload matching PDB+PE
    await client.patch(
        f"/api/v1/projects/{proj['id']}",
        json={"settings_json": {"ai_triage_mode": "MANUAL"}},
    )
    pdb = fake_toolchain.make_pdb(
        filename="LateMod.pdb",
        debug_id=did,
        code_file="LateMod.dll",
        code_id="65009999",
    )
    pe = fake_toolchain.make_pe(
        filename="LateMod.dll",
        code_id="65009999",
        debug_file="LateMod.pdb",
        debug_id=did,
    )
    assert (
        await helpers.upload_symbol(
            client,
            proj["key"],
            proj["api_key"],
            pdb,
            pdb_filename="LateMod.pdb",
            pe_bytes=pe,
            pe_filename="LateMod.dll",
        )
    ).status_code == 201

    resym = await client.post(
        "/api/v1/symbols/resymbolicate",
        json={"project_id": proj["id"], "debug_id": did},
    )
    assert resym.status_code == 202

    # Poll until missing_symbols_json clears and cluster changes
    deadline = asyncio.get_running_loop().time() + 15.0
    c_after = c_before
    while asyncio.get_running_loop().time() < deadline:
        c_after = (await client.get(f"/api/v1/crashes/{c_before['id']}")).json()
        if (
            c_after["status"] == "ANALYZED"
            and c_after["missing_symbols_json"] == []
            and c_after["cluster"]["id"] != old_cluster_id
        ):
            break
        await asyncio.sleep(0.05)

    assert c_after["missing_symbols_json"] == []
    assert c_after["cluster"]["id"] != old_cluster_id
    assert c_after["issue"] is None
    assert c_after["ai_verdict"] is None
    assert c_after["ai_confidence"] is None
    assert (await client.get(f"/api/v1/issues/{old_issue_id}")).json()["crash_count"] == 0


async def test_t5_19_concurrent_reprocess_triage_atomic_409(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    """Concurrent POST /reprocess(mode='triage') must atomically allow 1 (202) and 409 the rest."""
    proj = await helpers.create_project(
        client,
        key="t5-conc-reproc-triage",
        settings_json={"ai_triage_mode": "ALWAYS"},
    )
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Initial Reprocess Target")
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
    )
    assert up.status_code == 202
    cid = up.json()["crash_id"]
    initial = await helpers.wait_for_crash_status(client, cid)
    assert initial["status"] == "ANALYZED"
    assert len(initial["ai_runs"]) == 1
    assert len(mock_llm_server.requests) == 2
    issue_id = initial["issue"]["id"]

    mock_llm_server.set_delay(0.35)
    mock_llm_server.queue_verdict(
        verdict="KNOWN_ISSUE",
        matched_issue_id=issue_id,
        suggested_title="Initial Reprocess Target",
    )

    responses = await asyncio.gather(
        *[
            client.post(f"/api/v1/crashes/{cid}/reprocess", json={"mode": "triage"})
            for _ in range(4)
        ]
    )
    assert sorted(r.status_code for r in responses) == [202, 409, 409, 409]

    mock_llm_server.set_delay(0.0)
    final_detail = await helpers.wait_for_crash_status(client, cid)
    assert final_detail["status"] == "ANALYZED"
    assert len(final_detail["ai_runs"]) == 2
    assert len(mock_llm_server.requests) == 4
    assert (await client.get(f"/api/v1/issues/{issue_id}")).json()["crash_count"] == 1
