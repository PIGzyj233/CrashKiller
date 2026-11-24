from __future__ import annotations

import asyncio
import datetime as dt
import json
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import REPO_ROOT, SEED_SCRIPT, _load_backend_public

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
        await helpers.upload_crash(
            client, proj["key"], proj["api_key"], dmp, app_version="V" * 129
        )
    ).status_code == 422
    assert (
        await helpers.upload_crash(
            client, proj["key"], proj["api_key"], dmp, build_id="B" * 129
        )
    ).status_code == 422
    assert (
        await helpers.upload_crash(
            client, proj["key"], proj["api_key"], dmp, os_version="O" * 256
        )
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
        lg["filename"].endswith(".log") and len(lg["filename"]) == 512
        for lg in detail["logs"]
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


async def test_t5_10_per_project_retention_days_respected_by_gc(
    client: Any,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    """GC Step 2 TTL must honor Project.settings_json dump/log_retention_days overrides."""
    await client.put(
        "/api/v1/settings",
        json={"dump_retention_days": 14, "log_retention_days": 14},
    )
    p_long = await helpers.create_project(
        client,
        key="t5-ret-long",
        settings_json={
            "ai_triage_mode": "MANUAL",
            "dump_retention_days": 365,
            "log_retention_days": 365,
        },
    )
    p_short = await helpers.create_project(
        client,
        key="t5-ret-short",
        settings_json={
            "ai_triage_mode": "MANUAL",
            "dump_retention_days": 5,
            "log_retention_days": 5,
        },
    )

    dmp = fake_toolchain.make_dump()
    up_long = await helpers.upload_crash(
        client,
        p_long["key"],
        p_long["api_key"],
        dmp,
        log_files=[("long.log", b"2026-11-22 10:00:00.000 INFO long retain\n")],
    )
    up_short = await helpers.upload_crash(
        client,
        p_short["key"],
        p_short["api_key"],
        dmp,
        log_files=[("short.log", b"2026-11-22 10:00:00.000 INFO short retain\n")],
    )
    c_long_id = up_long.json()["crash_id"]
    l_long_id = up_long.json()["log_ids"][0]
    c_short_id = up_short.json()["crash_id"]
    l_short_id = up_short.json()["log_ids"][0]

    await helpers.wait_for_crash_status(client, c_long_id)
    await helpers.wait_for_crash_status(client, c_short_id)

    # Backdate both crashes by 20 days (> 5d short, > 14d global, < 365d long)
    aged_ts = (dt.datetime.now(dt.UTC) - dt.timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S.%f")
    with sqlite3.connect(isolated_env["db_path"]) as conn:
        conn.execute("UPDATE crash_reports SET created_at = ?", (aged_ts,))
        conn.commit()

    gc = (await client.post("/api/v1/settings/gc", json={"force_emergency": False})).json()
    assert gc["emergency_triggered"] is False
    assert gc["deleted_files_count"] >= 2

    # Long retention project artifacts preserved
    assert (await client.get(f"/api/v1/crashes/{c_long_id}/artifacts/dump")).status_code == 200
    assert (await client.get(f"/api/v1/logs/{l_long_id}/lines")).status_code == 200

    # Short retention project artifacts evicted
    assert (await client.get(f"/api/v1/crashes/{c_short_id}/artifacts/dump")).status_code == 404
    assert (await client.get(f"/api/v1/logs/{l_short_id}/lines")).status_code == 410


async def test_t5_11_gc_symbols_sym_lru_cleans_orphaned_raw_archives(
    client: Any,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    """Step 3e symbol LRU eviction must also unlink orphaned symbols/raw blobs."""
    await client.put(
        "/api/v1/settings",
        json={"archive_raw_symbols": True, "symbols_raw_quota_gb": 15.0},
    )
    proj = await helpers.create_project(client, key="t5-gc-sym-raw")
    did = "5B0011112222333344445555666677771"
    pdb = fake_toolchain.make_pdb(
        filename="RawOrphan.pdb",
        debug_id=did,
        code_file="RawOrphan.dll",
        code_id="6500BBBB",
    )
    pe = fake_toolchain.make_pe(
        filename="RawOrphan.dll",
        code_id="6500BBBB",
        debug_file="RawOrphan.pdb",
        debug_id=did,
    )
    up = await helpers.upload_symbol(
        client,
        proj["key"],
        proj["api_key"],
        pdb,
        pdb_filename="RawOrphan.pdb",
        pe_bytes=pe,
        pe_filename="RawOrphan.dll",
    )
    assert up.status_code == 201
    assert up.json()["raw_size"] > 0

    raw_dir = isolated_env["data_dir"] / "symbols" / "raw"
    sym_dir = isolated_env["data_dir"] / "symbols" / "sym"
    assert len(list(raw_dir.rglob("*.zst"))) >= 1
    assert len(list(sym_dir.rglob("*.sym"))) == 1

    # Shrink symbols_sym_quota_gb so Step 3b skips symbols/raw while Step 3e evicts Symbol
    await client.put(
        "/api/v1/settings",
        json={"symbols_sym_quota_gb": 1e-9, "gc_threshold_percent": 50.0},
    )
    gc = (await client.post("/api/v1/settings/gc", json={"force_emergency": False})).json()
    assert gc["deleted_files_count"] >= 2

    sym_list = (await client.get("/api/v1/symbols", params={"project_id": proj["id"]})).json()
    assert sym_list["total"] == 0
    assert list(sym_dir.rglob("*.sym")) == []
    assert list(raw_dir.rglob("*.zst")) == []


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
    log_text = "\n".join(
        [
            "2026-11-22 10:00:00.000 INFO STRAẞE path",
            "2026-11-22 10:00:01.000 INFO ſample descriptor",
            "2026-11-22 10:00:02.000 INFO Kelvin sensor",
            "2026-11-22 10:00:03.000 INFO İstanbul gateway",
            "",
            "2026-11-22 10:00:04.000 ERROR (?m) line anchor check",
        ]
    ) + "\n"

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
    assert (
        await client.get(f"/api/v1/logs/{empty_id}/search", params={"pattern": "foo"})
    ).json()["matches"] == []
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


def test_t5_17_cli_multiarch_symbol_dir_no_cross_arch_pe_pairing(
    live_server: str,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    """CLI upload-symbols --dir must not pair x86 PDB with x64 PE from another directory."""
    proj = httpx.post(
        f"{live_server}/api/v1/projects",
        json={"key": "t5-cli-arch", "name": "CLI MultiArch"},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()

    did_x86 = "5C0000000000000000000000000000861"
    did_x64 = "5C0000000000000000000000000000641"
    root = tmp_path / "multiarch"
    fake_toolchain.make_pdb(
        filename="game.pdb",
        debug_id=did_x86,
        code_file="game.dll",
        code_id="C0DE0086",
        write_to=root / "x86" / "game.pdb",
    )
    fake_toolchain.make_pdb(
        filename="game.pdb",
        debug_id=did_x64,
        code_file="game.dll",
        code_id="C0DE0064",
        write_to=root / "x64" / "game.pdb",
    )
    fake_toolchain.make_pe(
        filename="game.dll",
        code_id="C0DE0064",
        debug_file="game.pdb",
        debug_id=did_x64,
        write_to=root / "x64" / "game.dll",
    )

    res = helpers.run_cli(
        [
            "upload-symbols",
            "--server",
            live_server,
            "--project",
            proj["key"],
            "--api-key",
            proj["api_key"],
            "--dir",
            str(root),
            "--app-version",
            "1.0.0",
        ],
        env=isolated_env["proc_env"],
    )
    assert res.returncode == 0, res.stderr

    items = httpx.get(
        f"{live_server}/api/v1/symbols",
        params={"project_id": proj["id"]},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()["items"]
    by_did = {item["debug_id"]: item for item in items}
    assert by_did[did_x86]["has_pe_unwind"] is False
    assert by_did[did_x64]["has_pe_unwind"] is True


async def test_t5_18_seed_script_and_cpp_uploader_strict_compile(
    isolated_env: dict[str, Any],
    tmp_path: Path,
) -> None:
    """seed_demo_data.py runs idempotently twice; C++17 uploader compiles strictly."""
    for run_num in (1, 2):
        proc = subprocess.run(
            [sys.executable, str(SEED_SCRIPT)],
            env=isolated_env["proc_env"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, f"Seed run {run_num} failed: {proc.stderr}\n{proc.stdout}"

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
                dash = (await ac.get("/api/v1/dashboard")).json()
                assert dash["kpis"]["total_crashes"] == 4
                assert dash["kpis"]["total_symbols"] == 3

                crashes = (await ac.get("/api/v1/crashes")).json()["items"]
                assert len(crashes) == 4
                for c in crashes:
                    detail = (await ac.get(f"/api/v1/crashes/{c['id']}")).json()
                    for run in detail["ai_runs"]:
                        for snip in (run.get("result_json") or {}).get("evidence_log_snippets", []):
                            lines_resp = await ac.get(
                                f"/api/v1/logs/{snip['log_id']}/lines",
                                params={"start": snip["start_line"], "end": snip["end_line"]},
                            )
                            assert lines_resp.status_code == 200
                            assert len(lines_resp.json()["lines"]) >= 1
    finally:
        get_settings.cache_clear()

    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is not None:
        cpp_src = REPO_ROOT / "cli" / "cpp_uploader_example" / "main.cpp"
        cpp_bin = tmp_path / "cpp_uploader_check"
        comp = subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-Wall",
                "-Wextra",
                "-Werror",
                str(cpp_src),
                "-o",
                str(cpp_bin),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert comp.returncode == 0, f"C++ compile failed: {comp.stderr}"
        cli_check = subprocess.run(
            [str(cpp_bin)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert cli_check.returncode == 2


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


async def test_t5_20_symbol_upload_oversized_filenames_clamped_and_served(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    """Uploading symbols with >255-byte pdb_file/pe_file filenames must clamp cleanly (201)."""
    await client.put("/api/v1/settings", json={"archive_raw_symbols": True})
    proj = await helpers.create_project(client, key="t5-long-sym-name")
    did = "5D0011112222333344445555666677771"
    pdb = fake_toolchain.make_pdb(
        filename="LongName.pdb",
        debug_id=did,
        code_file="LongName.dll",
        code_id="6500DDDD",
    )
    pe = fake_toolchain.make_pe(
        filename="LongName.dll",
        code_id="6500DDDD",
        debug_file="LongName.pdb",
        debug_id=did,
    )

    up = await helpers.upload_symbol(
        client,
        proj["key"],
        proj["api_key"],
        pdb,
        pdb_filename=("a" * 300) + ".pdb",
        pe_bytes=pe,
        pe_filename=("b" * 300) + ".dll",
    )
    assert up.status_code == 201, up.text
    body = up.json()
    assert body["has_pe_unwind"] is True
    assert body["raw_size"] > 0

    srv_pdb = await client.get(
        f"/symsrv/{body['debug_file']}/{body['debug_id']}/{body['debug_file']}"
    )
    srv_pe = await client.get(
        f"/symsrv/{body['code_file']}/{body['code_id']}/{body['code_file']}"
    )
    assert srv_pdb.status_code == 200
    assert srv_pe.status_code == 200


async def test_t5_21_gc_symbols_raw_lru_respects_cross_project_refcount(
    client: Any,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    """Step 3b symbols/raw GC must evict unshared older raw file and keep shared /symsrv 200."""
    await client.put(
        "/api/v1/settings",
        json={
            "archive_raw_symbols": True,
            "symbols_raw_quota_gb": 15.0,
            "gc_threshold_percent": 50.0,
        },
    )
    p_a = await helpers.create_project(client, key="t5-raw-ref-a")
    p_b = await helpers.create_project(client, key="t5-raw-ref-b")
    p_c = await helpers.create_project(client, key="t5-raw-ref-c")

    did_shared = "5E0011112222333344445555666677771"
    did_old = "5F0011112222333344445555666677771"
    pdb_shared = fake_toolchain.make_pdb(
        filename="SharedMod.pdb",
        debug_id=did_shared,
        code_file="SharedMod.dll",
        code_id="6500EEEE",
    )
    pdb_old = fake_toolchain.make_pdb(
        filename="OldMod.pdb",
        debug_id=did_old,
        code_file="OldMod.dll",
        code_id="6500FFFF",
    )

    s_a = (
        await helpers.upload_symbol(
            client, p_a["key"], p_a["api_key"], pdb_shared, pdb_filename="SharedMod.pdb"
        )
    ).json()
    s_c = (
        await helpers.upload_symbol(
            client, p_c["key"], p_c["api_key"], pdb_old, pdb_filename="OldMod.pdb"
        )
    ).json()
    s_b = (
        await helpers.upload_symbol(
            client, p_b["key"], p_b["api_key"], pdb_shared, pdb_filename="SharedMod.pdb"
        )
    ).json()

    now = dt.datetime.now(dt.UTC)
    ts_a = (now - dt.timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S.%f")
    ts_c = (now - dt.timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S.%f")
    ts_b = now.strftime("%Y-%m-%d %H:%M:%S.%f")
    with sqlite3.connect(isolated_env["db_path"]) as conn:
        for sym_id_hex, ts_val in (
            (s_a["id"].replace("-", ""), ts_a),
            (s_c["id"].replace("-", ""), ts_c),
            (s_b["id"].replace("-", ""), ts_b),
        ):
            conn.execute(
                "UPDATE symbols SET last_used_at = ? WHERE id = ?",
                (ts_val, sym_id_hex),
            )
        conn.commit()

    shared_zst = isolated_env["data_dir"] / "symbols" / "raw" / did_shared / "SharedMod.pdb.zst"
    old_zst = isolated_env["data_dir"] / "symbols" / "raw" / did_old / "OldMod.pdb.zst"
    assert shared_zst.is_file() and old_zst.is_file()
    shared_sz = shared_zst.stat().st_size

    # Set target (50% of quota) to shared_sz + 8 bytes so only 1 file (OldMod) is evicted
    quota_gb = ((shared_sz + 8) * 2) / (1024**3)
    await client.put(
        "/api/v1/settings",
        json={"symbols_raw_quota_gb": quota_gb, "gc_threshold_percent": 50.0},
    )

    gc = (await client.post("/api/v1/settings/gc", json={"force_emergency": False})).json()
    assert gc["deleted_files_count"] >= 1

    assert (await client.get(f"/symsrv/OldMod.pdb/{did_old}/OldMod.pdb")).status_code == 404
    srv_shared = await client.get(f"/symsrv/SharedMod.pdb/{did_shared}/SharedMod.pdb")
    assert srv_shared.status_code == 200


async def test_t5_22_symbol_delete_cleans_all_raw_and_cli_three_dir_no_mispair(
    client: Any,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    """Symbol upgrade+delete cleans all symbols/raw .zst; CLI skips ambiguous 3-dir pairing."""
    await client.put("/api/v1/settings", json={"archive_raw_symbols": True})
    proj = await helpers.create_project(client, key="t5-clean-all-raw")
    did = "5A1111112222333344445555666677771"
    pdb = fake_toolchain.make_pdb(
        filename="CleanMod.pdb",
        debug_id=did,
        code_file="CleanMod.dll",
        code_id="6500AAAA",
    )
    pe = fake_toolchain.make_pe(
        filename="CleanMod.dll",
        code_id="6500AAAA",
        debug_file="CleanMod.pdb",
        debug_id=did,
    )

    up1 = await helpers.upload_symbol(
        client, proj["key"], proj["api_key"], pdb, pdb_filename="CleanMod.pdb"
    )
    assert up1.status_code == 201
    up2 = await helpers.upload_symbol(
        client,
        proj["key"],
        proj["api_key"],
        pdb,
        pdb_filename="CleanMod.pdb",
        pe_bytes=pe,
        pe_filename="CleanMod.dll",
    )
    assert up2.status_code == 201
    sym_id = up2.json()["id"]

    raw_dir = isolated_env["data_dir"] / "symbols" / "raw"
    assert len(list(raw_dir.rglob("*.zst"))) == 2
    assert (await client.delete(f"/api/v1/symbols/{sym_id}")).status_code == 204
    assert list(raw_dir.rglob("*.zst")) == []
    assert (await client.get(f"/symsrv/CleanMod.pdb/{did}/CleanMod.pdb")).status_code == 404
    assert (await client.get("/symsrv/CleanMod.dll/6500AAAA/CleanMod.dll")).status_code == 404

    # CLI 3-dir check: x86/foo.pdb + x64/foo.pdb + shared/foo.dll must NOT mis-pair
    three_dir = tmp_path / "three_dir"
    (three_dir / "x86").mkdir(parents=True)
    (three_dir / "x64").mkdir(parents=True)
    (three_dir / "shared").mkdir(parents=True)
    (three_dir / "x86" / "foo.pdb").write_bytes(b"PDB86")
    (three_dir / "x64" / "foo.pdb").write_bytes(b"PDB64")
    (three_dir / "shared" / "foo.dll").write_bytes(b"DLL")

    dry = helpers.run_cli(
        [
            "upload-symbols",
            "--server",
            "http://127.0.0.1:1",
            "--project",
            "ignored",
            "--api-key",
            "ignored",
            "--dir",
            str(three_dir),
            "--app-version",
            "1.0.0",
            "--dry-run",
        ],
        env=isolated_env["proc_env"],
    )
    assert dry.returncode == 0, dry.stderr
    assert "pe=(none)" in dry.stdout
    assert "foo.dll" not in dry.stdout
