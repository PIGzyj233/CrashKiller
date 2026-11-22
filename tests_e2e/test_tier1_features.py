from __future__ import annotations

import ast
import asyncio
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.tier1

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Area 1: Health, SymSrv & Auth (T1-01 .. T1-05)
# ---------------------------------------------------------------------------


async def test_t1_01_health_liveness(client: Any) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "ok"


async def test_t1_02_symsrv_pingme(client: Any) -> None:
    resp = await client.get("/symsrv/pingme.txt")
    assert resp.status_code == 200
    assert resp.text == ""


async def test_t1_03_symsrv_index2_returns_404(client: Any) -> None:
    resp = await client.get("/symsrv/index2.txt")
    assert resp.status_code == 404


async def test_t1_04_symsrv_download_get_and_head(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    put_resp = await client.put("/api/v1/settings", json={"archive_raw_symbols": True})
    assert put_resp.status_code == 200

    proj = await helpers.create_project(client, key="symsrv-proj")
    debug_id = "46A0ADB3F299A70B4C4C44205044422E1"
    pdb_bytes = fake_toolchain.make_pdb(
        filename="MyApp.pdb",
        debug_id=debug_id,
        code_file="MyApp.exe",
        code_id="64EC878F867C000",
        extra_bytes=b"\x01\x02\x03RAW_PDB_PAYLOAD",
    )
    pe_bytes = fake_toolchain.make_pe(
        filename="MyApp.exe",
        code_id="64EC878F867C000",
        debug_file="MyApp.pdb",
        debug_id=debug_id,
        extra_bytes=b"\x04\x05\x06RAW_PE_PAYLOAD",
    )

    up_resp = await helpers.upload_symbol(
        client,
        proj["key"],
        proj["api_key"],
        pdb_bytes=pdb_bytes,
        pdb_filename="MyApp.pdb",
        pe_bytes=pe_bytes,
        pe_filename="MyApp.exe",
    )
    assert up_resp.status_code == 201

    head_resp = await client.head(f"/symsrv/MyApp.pdb/{debug_id}/MyApp.pdb")
    assert head_resp.status_code == 200
    assert head_resp.content == b""

    get_resp = await client.get(f"/symsrv/MyApp.pdb/{debug_id}/MyApp.pdb")
    assert get_resp.status_code == 200
    assert get_resp.content == pdb_bytes


async def test_t1_05_dual_upload_auth_header_and_form(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="dual-auth")
    pdb_bytes = fake_toolchain.make_pdb(
        filename="DualAuth.pdb",
        debug_id="AAAA11112222333344445555666677771",
        code_file="DualAuth.exe",
        code_id="112233445566",
    )
    sym_resp = await helpers.upload_symbol(
        client,
        proj["key"],
        proj["api_key"],
        pdb_bytes=pdb_bytes,
        pdb_filename="DualAuth.pdb",
        use_header=True,
    )
    assert sym_resp.status_code == 201

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="DualAuth Crash")
    dmp_bytes = fake_toolchain.make_dump()
    crash_resp = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        dump_bytes=dmp_bytes,
        use_header=False,
    )
    assert crash_resp.status_code == 202
    assert "crash_id" in crash_resp.json()


# ---------------------------------------------------------------------------
# Area 2: Projects (T1-06 .. T1-10)
# ---------------------------------------------------------------------------


async def test_t1_06_create_project(client: Any) -> None:
    resp = await client.post(
        "/api/v1/projects",
        json={
            "key": "game-x",
            "name": "Game X",
            "settings_json": {"ai_triage_mode": "SMART_TRIAGE"},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    uuid.UUID(str(data["id"]))
    assert data["key"] == "game-x"
    assert data["name"] == "Game X"
    assert data["settings_json"]["ai_triage_mode"] == "SMART_TRIAGE"
    assert isinstance(data["api_key"], str) and len(data["api_key"]) >= 16
    assert "created_at" in data
    assert "api_key_hash" not in data


async def test_t1_07_list_projects_hides_secrets(client: Any, helpers: Any) -> None:
    created = await helpers.create_project(client, key="list-proj", name="List Proj")
    resp = await client.get("/api/v1/projects")
    assert resp.status_code == 200
    items = resp.json()
    assert isinstance(items, list)
    match = [p for p in items if p["id"] == created["id"]]
    assert len(match) == 1
    assert "api_key" not in match[0]
    assert "api_key_hash" not in match[0]
    assert match[0]["key"] == "list-proj"


async def test_t1_08_get_single_project(client: Any, helpers: Any) -> None:
    created = await helpers.create_project(client, key="single-proj", name="Single Proj")
    resp = await client.get(f"/api/v1/projects/{created['id']}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == created["id"]
    assert data["key"] == "single-proj"
    assert data["name"] == "Single Proj"
    assert "api_key_hash" not in data


async def test_t1_09_update_project(client: Any, helpers: Any) -> None:
    created = await helpers.create_project(client, key="upd-proj", name="Old Name")
    patch_resp = await client.patch(
        f"/api/v1/projects/{created['id']}",
        json={
            "name": "Game X2",
            "settings_json": {"noise_modules": ["custom.dll"], "ai_triage_mode": "ALWAYS"},
        },
    )
    assert patch_resp.status_code == 200
    patched = patch_resp.json()
    assert patched["name"] == "Game X2"
    assert patched["settings_json"]["noise_modules"] == ["custom.dll"]
    assert patched["settings_json"]["ai_triage_mode"] == "ALWAYS"

    get_resp = await client.get(f"/api/v1/projects/{created['id']}")
    assert get_resp.status_code == 200
    assert get_resp.json()["name"] == "Game X2"


async def test_t1_10_rotate_project_api_key(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    created = await helpers.create_project(client, key="rot-proj", name="Rotate Proj")
    old_key = created["api_key"]

    rot_resp = await client.post(f"/api/v1/projects/{created['id']}/rotate-key")
    assert rot_resp.status_code == 200
    rot_data = rot_resp.json()
    new_key = rot_data["api_key"]
    assert new_key and new_key != old_key
    assert rot_data["id"] == created["id"]

    pdb = fake_toolchain.make_pdb(debug_id="BBBB11112222333344445555666677771")
    up_resp = await helpers.upload_symbol(client, created["key"], new_key, pdb_bytes=pdb)
    assert up_resp.status_code == 201


# ---------------------------------------------------------------------------
# Area 3: Symbols (T1-11 .. T1-16)
# ---------------------------------------------------------------------------


async def test_t1_11_upload_pdb_plus_pe(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="sym-pe")
    pdb_bytes = fake_toolchain.make_pdb(
        filename="Engine.pdb",
        debug_id="CCCC11112222333344445555666677771",
        code_file="Engine.dll",
        code_id="650011223344",
    )
    pe_bytes = fake_toolchain.make_pe(
        filename="Engine.dll",
        code_id="650011223344",
        debug_file="Engine.pdb",
        debug_id="CCCC11112222333344445555666677771",
    )
    resp = await helpers.upload_symbol(
        client,
        proj["key"],
        proj["api_key"],
        pdb_bytes=pdb_bytes,
        pdb_filename="Engine.pdb",
        pe_bytes=pe_bytes,
        pe_filename="Engine.dll",
        app_version="1.0.0",
    )
    assert resp.status_code == 201
    sym = resp.json()
    assert sym["has_pe_unwind"] is True
    assert sym["debug_file"] == "Engine.pdb"
    assert sym["debug_id"] == "CCCC11112222333344445555666677771"
    assert sym["code_file"] == "Engine.dll"
    assert sym["code_id"] == "650011223344"
    assert sym["sym_size"] > 0
    assert sym["deduplicated"] is False


async def test_t1_12_upload_pdb_only_fallback(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="sym-pdb-only")
    pdb_bytes = fake_toolchain.make_pdb(
        filename="Fallback.pdb",
        debug_id="DDDD11112222333344445555666677771",
        code_file="Fallback.exe",
        code_id="650011229999",
    )
    resp = await helpers.upload_symbol(
        client,
        proj["key"],
        proj["api_key"],
        pdb_bytes=pdb_bytes,
        pdb_filename="Fallback.pdb",
        pe_bytes=None,
        app_version="1.0.1",
        project_id=proj["id"],
    )
    assert resp.status_code == 201
    sym = resp.json()
    assert sym["has_pe_unwind"] is False
    assert sym["sym_size"] > 0
    assert sym["deduplicated"] is False


async def test_t1_13_list_and_filter_symbols(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="sym-filter")
    pdb1 = fake_toolchain.make_pdb(filename="Alpha.pdb", debug_id="111111111111111111111111111111111")
    pe1 = fake_toolchain.make_pe(filename="Alpha.exe", debug_file="Alpha.pdb", debug_id="111111111111111111111111111111111")
    pdb2 = fake_toolchain.make_pdb(filename="Beta.pdb", debug_id="222222222222222222222222222222221")

    r1 = await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb1, "Alpha.pdb", pe1, "Alpha.exe", app_version="1.0.0")
    r2 = await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb2, "Beta.pdb", None, app_version="2.0.0")
    assert r1.status_code == 201 and r2.status_code == 201

    resp = await client.get(
        "/api/v1/symbols",
        params={
            "project_id": proj["id"],
            "app_version": "1.0.0",
            "has_pe_unwind": "true",
            "q": "Alpha",
            "limit": 10,
            "offset": 0,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["debug_file"] == "Alpha.pdb"


async def test_t1_14_missing_symbols_queue(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="sym-missing")
    missing_id = "EEEE11112222333344445555666677771"
    dmp = fake_toolchain.make_dump(
        modules=[
            {
                "filename": "MissingMod.dll",
                "debug_file": "MissingMod.pdb",
                "debug_id": missing_id,
                "code_file": "MissingMod.dll",
                "code_id": "AAA111",
                "always_loaded": False,
            }
        ],
        threads=[
            {
                "thread_name": "MainThread",
                "frames": [
                    {"module": "MissingMod.dll", "function": "HiddenFunc()", "file": "src/m.cpp", "line": 12}
                ],
            }
        ],
    )
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="MissingMod Crash")
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)
    assert up.status_code == 202
    crash_id = up.json()["crash_id"]
    await helpers.wait_for_crash_status(client, crash_id)

    miss_resp = await client.get("/api/v1/symbols/missing", params={"project_id": proj["id"]})
    assert miss_resp.status_code == 200
    items = miss_resp.json()
    assert any(item["debug_id"] == missing_id and item["crash_count"] >= 1 for item in items)
    matched = [i for i in items if i["debug_id"] == missing_id][0]
    assert crash_id in [str(cid) for cid in matched["affected_crash_ids"]]


async def test_t1_15_trigger_resymbolication(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="sym-resym")
    missing_id = "FFFF11112222333344445555666677771"
    dmp = fake_toolchain.make_dump(
        modules=[
            {
                "filename": "GameCore.dll",
                "debug_file": "GameCore.pdb",
                "debug_id": missing_id,
                "code_file": "GameCore.dll",
                "code_id": "BBB222",
                "always_loaded": False,
            }
        ],
        threads=[
            {
                "thread_name": "MainThread",
                "frames": [
                    {"module": "GameCore.dll", "function": "GameCore::CrashHere()", "file": "src/core.cpp", "line": 77}
                ],
            }
        ],
    )
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Unresolved Crash")
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)
    crash_id = up.json()["crash_id"]
    before = await helpers.wait_for_crash_status(client, crash_id)
    assert len(before["missing_symbols_json"]) >= 1

    pdb = fake_toolchain.make_pdb(
        filename="GameCore.pdb",
        debug_id=missing_id,
        code_file="GameCore.dll",
        code_id="BBB222",
    )
    pe = fake_toolchain.make_pe(
        filename="GameCore.dll",
        code_id="BBB222",
        debug_file="GameCore.pdb",
        debug_id=missing_id,
    )
    sym_up = await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb, "GameCore.pdb", pe, "GameCore.dll")
    assert sym_up.status_code == 201

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Resolved Crash")
    resym = await client.post(
        "/api/v1/symbols/resymbolicate",
        json={"project_id": proj["id"], "debug_id": missing_id},
    )
    assert resym.status_code == 202
    assert resym.json()["queued_count"] >= 1
    assert crash_id in [str(c) for c in resym.json()["crash_ids"]]

    after = await helpers.wait_for_crash_status(client, crash_id)
    for _ in range(150):
        if len(after["missing_symbols_json"]) == 0 and after["status"] == "ANALYZED":
            break
        await asyncio.sleep(0.1)
        r = await client.get(f"/api/v1/crashes/{crash_id}")
        after = r.json()
    assert after["status"] == "ANALYZED"
    assert after["missing_symbols_json"] == []
    top_frame = after["parsed_stacks_json"]["threads"][0]["frames"][0]
    assert top_frame["trust"] == "cfi"
    assert top_frame["function"] == "GameCore::CrashHere()"


async def test_t1_16_delete_symbol(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="sym-del")
    pdb = fake_toolchain.make_pdb(filename="DelMod.pdb", debug_id="999911112222333344445555666677771")
    up = await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb, "DelMod.pdb")
    assert up.status_code == 201
    sym_id = up.json()["id"]

    del_resp = await client.delete(f"/api/v1/symbols/{sym_id}")
    assert del_resp.status_code == 204
    assert del_resp.content == b""

    list_resp = await client.get("/api/v1/symbols", params={"project_id": proj["id"]})
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 0


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
    await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb, "MyApp.pdb", pe, "MyApp.exe")

    dmp = fake_toolchain.make_dump(crash_reason="EXCEPTION_ACCESS_VIOLATION_READ", crash_address="0x0000000000000010")
    log_bytes = helpers.make_mixed_encoding_log(num_lines=25)
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Detail Crash")
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp, log_files=[("app.log", log_bytes)])
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
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump())
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
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump())
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
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump(), log_files=[("l.log", log_bytes)]
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
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump(), log_files=[("t.log", log_bytes)]
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
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump(), log_files=[("s.log", log_bytes)]
    )
    log_id = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    resp = await client.get(
        f"/api/v1/logs/{log_id}/search",
        params={"pattern": "FATAL", "is_regex": "false", "max_matches": 50, "case_sensitive": "true"},
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
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump(), log_files=[("r.log", log_bytes)]
    )
    log_id = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    resp = await client.get(
        f"/api/v1/logs/{log_id}/search",
        params={"pattern": r"TextureCache.*null", "is_regex": "true", "max_matches": 50, "case_sensitive": "false"},
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
        {"filename": "ntdll.dll", "debug_file": "ntdll.pdb", "debug_id": "NTDLL1", "always_loaded": True},
        {"filename": "kernelbase.dll", "debug_file": "kernelbase.pdb", "debug_id": "KBASE1", "always_loaded": True},
        {"filename": "MyApp.exe", "debug_file": "MyApp.pdb", "debug_id": "MYAPP1", "always_loaded": True},
    ]
    dmp1 = fake_toolchain.make_dump(
        modules=modules,
        threads=[
            {
                "thread_name": "Main",
                "frames": [
                    {"module": "ntdll.dll", "function": "KiUserExceptionDispatcher", "offset": "0x7ff900001000"},
                    {"module": "kernelbase.dll", "function": "RaiseException", "offset": "0x7ff900002000"},
                    {"module": "MyApp.exe", "function": "Renderer::SubmitDrawCall(Mesh*)", "offset": "0x7ff700011000"},
                    {"module": "MyApp.exe", "function": "App::Tick(float)", "offset": "0x7ff700012000"},
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
                    {"module": "ntdll.dll", "function": "RtlDispatchException", "offset": "0x7ff900009999"},
                    {"module": "MyApp.exe", "function": "Renderer::SubmitDrawCall(Mesh*)", "offset": "0x7ff700099000"},
                    {"module": "MyApp.exe", "function": "App::Tick(float)", "offset": "0x7ff70009a000"},
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
        params={"project_id": proj["id"], "status": "KNOWN", "component": "Rendering", "q": "Texture"},
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
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump())
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
    d1 = fake_toolchain.make_dump(threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": "FuncA()"}]}])
    d2 = fake_toolchain.make_dump(threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": "FuncB()"}]}])

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Issue A", key_log_patterns=["PAT_A"])
    u1 = await helpers.upload_crash(client, proj["key"], proj["api_key"], d1)
    c1 = await helpers.wait_for_crash_status(client, u1.json()["crash_id"])

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Issue B", key_log_patterns=["PAT_B"])
    u2 = await helpers.upload_crash(client, proj["key"], proj["api_key"], d2)
    c2 = await helpers.wait_for_crash_status(client, u2.json()["crash_id"])

    target_id = c1["issue"]["id"]
    src_id = c2["issue"]["id"]
    assert target_id != src_id

    m = await client.post("/api/v1/issues/merge", json={"target_issue_id": target_id, "source_issue_ids": [src_id]})
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
    d1 = fake_toolchain.make_dump(threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": "SplitA()"}]}])
    d2 = fake_toolchain.make_dump(threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": "SplitB()"}]}])

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Issue 1")
    c1 = await helpers.wait_for_crash_status(
        client, (await helpers.upload_crash(client, proj["key"], proj["api_key"], d1)).json()["crash_id"]
    )
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Issue 2")
    c2 = await helpers.wait_for_crash_status(
        client, (await helpers.upload_crash(client, proj["key"], proj["api_key"], d2)).json()["crash_id"]
    )

    t_id = c1["issue"]["id"]
    s_id = c2["issue"]["id"]
    await client.post("/api/v1/issues/merge", json={"target_issue_id": t_id, "source_issue_ids": [s_id]})

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
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump())
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
    def _dynamic_handler(req_json: dict[str, Any], headers: dict[str, str], idx: int) -> dict[str, Any]:
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
                    [("get_log_summary", {"log_id": lid}), ("search_log", {"log_id": lid, "pattern": "TextureCache"})]
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
            evidence_log_snippets=[{"log_id": lid, "start_line": 22, "end_line": 22, "relevance": "null deref"}],
        )
        return mock_llm_server._build_completion_payload(
            model=req_json["model"], content=verdict["content"], finish_reason="stop"
        )

    mock_llm_server.set_handler(_dynamic_handler)
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump(), log_files=[("app.log", log_bytes)]
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
        m["content"] for m in mock_llm_server.requests[2]["body"]["messages"] if m.get("role") == "tool"
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
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump())
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
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump())
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


# ---------------------------------------------------------------------------
# Area 8: Settings, Quotas & GC (T1-42 .. T1-46)
# ---------------------------------------------------------------------------


async def test_t1_42_get_system_settings(client: Any) -> None:
    resp = await client.get("/api/v1/settings")
    assert resp.status_code == 200
    data = resp.json()
    expected_keys = {
        "llm_base_url",
        "llm_api_key_set",
        "llm_api_key_masked",
        "llm_model",
        "ai_triage_mode",
        "ai_max_steps",
        "ai_timeout_seconds",
        "ai_tool_output_max_chars",
        "total_disk_quota_gb",
        "host_safety_floor_gb",
        "db_quota_gb",
        "symbols_sym_quota_gb",
        "symbols_public_cache_quota_gb",
        "symbols_raw_quota_gb",
        "logs_quota_gb",
        "dumps_quota_gb",
        "staging_quota_gb",
        "gc_threshold_percent",
        "log_retention_days",
        "dump_retention_days",
        "staging_ttl_hours",
        "archive_raw_symbols",
        "enable_public_symbols",
        "public_symbol_urls",
        "default_noise_modules",
        "default_noise_functions",
    }
    assert expected_keys.issubset(set(data.keys()))
    assert "llm_api_key" not in data


async def test_t1_43_update_system_settings(client: Any) -> None:
    resp = await client.put(
        "/api/v1/settings",
        json={
            "llm_model": "gpt-4o",
            "ai_max_steps": 6,
            "total_disk_quota_gb": 100.0,
            "host_safety_floor_gb": 40.0,
            "log_retention_days": 15,
        },
    )
    assert resp.status_code == 200
    put_body = resp.json()
    assert put_body["llm_model"] == "gpt-4o"
    assert put_body["ai_max_steps"] == 6
    assert put_body["total_disk_quota_gb"] == 100.0
    assert put_body["host_safety_floor_gb"] == 40.0
    assert put_body["log_retention_days"] == 15

    get_body = (await client.get("/api/v1/settings")).json()
    assert get_body["llm_model"] == "gpt-4o"
    assert get_body["total_disk_quota_gb"] == 100.0


async def test_t1_44_manual_gc_execution(client: Any) -> None:
    resp = await client.post("/api/v1/settings/gc", json={"force_emergency": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["freed_bytes"] >= 0
    assert body["deleted_files_count"] >= 0
    assert body["skipped_pinned_count"] >= 0
    assert body["emergency_triggered"] is False
    assert "partitions" in body


async def test_t1_45_emergency_gc_evicts_unpinned_artifacts(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="gc-evict")
    log_bytes = helpers.make_mixed_encoding_log(num_lines=80)
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(extra_bytes=b"Z" * 4096),
        log_files=[("gc.log", log_bytes)],
    )
    crash_id = up.json()["crash_id"]
    await helpers.wait_for_crash_status(client, crash_id)

    gc_resp = await client.post("/api/v1/settings/gc", json={"force_emergency": True})
    assert gc_resp.status_code == 200
    report = gc_resp.json()
    assert report["emergency_triggered"] is True
    assert report["deleted_files_count"] >= 1
    assert report["freed_bytes"] > 0


async def test_t1_46_dynamic_llm_api_key_masking(client: Any) -> None:
    secret = "sk-test-secret-9876"
    put_resp = await client.put("/api/v1/settings", json={"llm_api_key": secret})
    assert put_resp.status_code == 200

    get_resp = await client.get("/api/v1/settings")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["llm_api_key_set"] is True
    assert secret not in str(data["llm_api_key_masked"])
    assert data["llm_api_key_masked"]


# ---------------------------------------------------------------------------
# Area 9: Dashboard (T1-47 .. T1-51)
# ---------------------------------------------------------------------------


async def test_t1_47_global_dashboard_kpis(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="dash-kpi")
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE")
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump())
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    resp = await client.get("/api/v1/dashboard", params={"days": 14})
    assert resp.status_code == 200
    body = resp.json()
    for key in ("kpis", "crash_trend", "verdict_distribution", "issue_status_distribution", "top_missing_symbols", "storage"):
        assert key in body
    assert body["kpis"]["total_crashes"] >= 1
    assert body["kpis"]["analyzed_crashes"] >= 1


async def test_t1_48_project_scoped_dashboard_filtering(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    p_a = await helpers.create_project(client, key="dash-a")
    p_b = await helpers.create_project(client, key="dash-b")

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE")
    u_a = await helpers.upload_crash(client, p_a["key"], p_a["api_key"], fake_toolchain.make_dump())
    await helpers.wait_for_crash_status(client, u_a.json()["crash_id"])

    for _ in range(2):
        mock_llm_server.queue_verdict(verdict="NEW_ISSUE")
        u_b = await helpers.upload_crash(client, p_b["key"], p_b["api_key"], fake_toolchain.make_dump())
        await helpers.wait_for_crash_status(client, u_b.json()["crash_id"])

    dash_a = (await client.get("/api/v1/dashboard", params={"project_id": p_a["id"], "days": 7})).json()
    assert dash_a["kpis"]["total_crashes"] == 1


async def test_t1_49_dashboard_crash_trend_windows(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="dash-trend")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump())
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    d7 = (await client.get("/api/v1/dashboard", params={"days": 7})).json()
    d30 = (await client.get("/api/v1/dashboard", params={"days": 30})).json()
    assert isinstance(d7["crash_trend"], list) and len(d7["crash_trend"]) >= 1
    assert isinstance(d30["crash_trend"], list) and len(d30["crash_trend"]) >= len(d7["crash_trend"])


async def test_t1_50_dashboard_verdict_and_issue_distributions(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="dash-dist")
    known = (
        await client.post("/api/v1/issues", json={"project_id": proj["id"], "title": "Fixed One", "status": "FIXED"})
    ).json()

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="New One")
    c1 = await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump())
    await helpers.wait_for_crash_status(client, c1.json()["crash_id"])

    mock_llm_server.queue_verdict(verdict="KNOWN_ISSUE", matched_issue_id=known["id"])
    d2 = fake_toolchain.make_dump(threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": "Other()"}]}])
    c2 = await helpers.upload_crash(client, proj["key"], proj["api_key"], d2)
    await helpers.wait_for_crash_status(client, c2.json()["crash_id"])

    dash = (await client.get("/api/v1/dashboard", params={"project_id": proj["id"], "days": 14})).json()
    v_str = str(dash["verdict_distribution"])
    i_str = str(dash["issue_status_distribution"])
    assert "NEW_ISSUE" in v_str and "KNOWN_ISSUE" in v_str
    assert "NEW" in i_str and "FIXED" in i_str


async def test_t1_51_dashboard_storage_watermark_and_top_missing(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="dash-store")
    dmp = fake_toolchain.make_dump(
        modules=[
            {
                "filename": "NoSym.dll",
                "debug_file": "NoSym.pdb",
                "debug_id": "123400000000000000000000000000011",
                "always_loaded": False,
            }
        ],
        threads=[{"thread_name": "M", "frames": [{"module": "NoSym.dll", "function": "F()"}]}],
    )
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    dash = (await client.get("/api/v1/dashboard", params={"project_id": proj["id"]})).json()
    storage = dash["storage"]
    assert isinstance(storage["safety_floor_breached"], bool)
    assert len(dash["top_missing_symbols"]) >= 1
    assert dash["top_missing_symbols"][0]["debug_file"] == "NoSym.pdb"


# ---------------------------------------------------------------------------
# Area 10: CLI & Repo / Frontend / Infra Contracts (T1-52 .. T1-57)
# ---------------------------------------------------------------------------


def test_t1_52_cli_upload_symbols_paired_and_recursive(
    live_server: str,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    import httpx

    proj = httpx.post(
        f"{live_server}/api/v1/projects",
        json={"key": "cli-sym", "name": "CLI Sym"},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()

    build_dir = tmp_path / "build_out"
    sub_dir = build_dir / "nested"
    fake_toolchain.make_pe(
        filename="App.exe",
        code_id="C0DE0001",
        debug_file="App.pdb",
        debug_id="AAA000000000000000000000000000011",
        write_to=build_dir / "App.exe",
    )
    fake_toolchain.make_pdb(
        filename="App.pdb",
        debug_id="AAA000000000000000000000000000011",
        code_file="App.exe",
        code_id="C0DE0001",
        write_to=build_dir / "App.pdb",
    )
    fake_toolchain.make_pdb(
        filename="Lib.pdb",
        debug_id="BBB000000000000000000000000000011",
        code_file="Lib.dll",
        code_id="C0DE0002",
        write_to=sub_dir / "Lib.pdb",
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
            str(build_dir),
            "--app-version",
            "1.0.0",
        ],
        env=isolated_env["proc_env"],
    )
    assert res.returncode == 0, f"CLI stderr: {res.stderr}\nstdout: {res.stdout}"

    syms = httpx.get(
        f"{live_server}/api/v1/symbols",
        params={"project_id": proj["id"]},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()
    assert syms["total"] == 2
    by_file = {s["debug_file"]: s for s in syms["items"]}
    assert by_file["App.pdb"]["has_pe_unwind"] is True
    assert by_file["Lib.pdb"]["has_pe_unwind"] is False


def test_t1_53_cli_upload_crash_wait(
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
        json={"key": "cli-crash", "name": "CLI Crash"},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()
    dmp_path = fake_toolchain.fixtures_dir / "cli_test.dmp"
    fake_toolchain.make_dump(write_to=dmp_path)
    log_path = tmp_path / "cli_test.log"
    log_path.write_bytes(helpers.make_mixed_encoding_log(num_lines=20))
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="CLI Wait Crash")

    res = helpers.run_cli(
        [
            "upload-crash",
            "--server",
            live_server,
            "--project",
            proj["key"],
            "--api-key",
            proj["api_key"],
            "--dump",
            str(dmp_path),
            "--log",
            str(log_path),
            "--app-version",
            "1.0.0",
            "--wait",
        ],
        env=isolated_env["proc_env"],
    )
    assert res.returncode == 0, f"CLI stderr: {res.stderr}\nstdout: {res.stdout}"
    combined = res.stdout + res.stderr
    assert "ANALYZED" in combined
    assert "NEW_ISSUE" in combined


def test_t1_54_cli_upload_symbols_dry_run(
    live_server: str,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    import httpx

    proj = httpx.post(
        f"{live_server}/api/v1/projects",
        json={"key": "cli-dry", "name": "CLI Dry"},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()
    build_dir = tmp_path / "dry_build"
    fake_toolchain.make_pdb(write_to=build_dir / "Dry.pdb")

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
            str(build_dir),
            "--app-version",
            "1.0.0",
            "--dry-run",
        ],
        env=isolated_env["proc_env"],
    )
    assert res.returncode == 0, f"CLI stderr: {res.stderr}\nstdout: {res.stdout}"
    syms = httpx.get(
        f"{live_server}/api/v1/symbols",
        params={"project_id": proj["id"]},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()
    assert syms["total"] == 0


def test_t1_55_repo_env_docker_nginx_and_alembic_alignment() -> None:
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    required_env_keys = [
        "APP_NAME", "APP_VERSION", "DEBUG", "API_V1_PREFIX", "CORS_ORIGINS", "HTTP_PORT",
        "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "DATABASE_URL", "REDIS_URL",
        "CRASHKILLER_DATA_PATH", "DATA_DIR", "TOTAL_DISK_QUOTA_GB", "HOST_SAFETY_FLOOR_GB",
        "DB_QUOTA_GB", "SYMBOLS_SYM_QUOTA_GB", "SYMBOLS_PUBLIC_CACHE_QUOTA_GB",
        "SYMBOLS_RAW_QUOTA_GB", "LOGS_QUOTA_GB", "DUMPS_QUOTA_GB", "STAGING_QUOTA_GB",
        "GC_THRESHOLD_PERCENT", "LOG_RETENTION_DAYS", "DUMP_RETENTION_DAYS", "STAGING_TTL_HOURS",
        "ARCHIVE_RAW_SYMBOLS", "DUMP_SYMS_BIN", "MINIDUMP_STACKWALK_BIN",
        "ENABLE_PUBLIC_SYMBOLS", "PUBLIC_SYMBOL_URLS", "TOOL_TIMEOUT_SECONDS",
        "LOG_CHUNK_SIZE_BYTES", "LOG_ZSTD_LEVEL", "DUMP_ZSTD_LEVEL",
        "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "AI_TRIAGE_MODE",
        "AI_MAX_STEPS", "AI_TIMEOUT_SECONDS", "AI_TOOL_OUTPUT_MAX_CHARS",
    ]
    for key in required_env_keys:
        assert f"{key}=" in env_example, f"Missing {key} in .env.example"

    compose_file = REPO_ROOT / "docker-compose.yml"
    nginx_file = REPO_ROOT / "docker" / "nginx.conf"
    alembic_file = REPO_ROOT / "backend" / "alembic" / "versions" / "0001_initial_schema.py"
    backend_dockerfile = REPO_ROOT / "docker" / "backend.Dockerfile"
    frontend_dockerfile = REPO_ROOT / "docker" / "frontend.Dockerfile"
    pyproject_file = REPO_ROOT / "backend" / "pyproject.toml"
    config_file = REPO_ROOT / "backend" / "app" / "config.py"
    require_app = os.environ.get("CRASHKILLER_E2E_REQUIRE_APP", "").strip() == "1"

    core_infra = (compose_file, nginx_file, alembic_file)
    missing = [str(p) for p in core_infra if not p.exists()]
    if missing:
        msg = f"Infra files not yet implemented: {', '.join(missing)}"
        if require_app:
            pytest.fail(msg)
        pytest.skip(msg)

    compose_txt = compose_file.read_text(encoding="utf-8")
    for svc in ("nginx", "api", "worker", "postgres", "redis"):
        assert svc in compose_txt, f"Missing service {svc} in docker-compose.yml"

    mem_limits_mb: list[float] = []
    for m in re.finditer(r"mem_limit:\s*([0-9]+(?:\.[0-9]+)?)\s*([mgkMGK])?", compose_txt):
        val = float(m.group(1))
        unit = (m.group(2) or "b").lower()
        if unit == "g":
            mem_limits_mb.append(val * 1024.0)
        elif unit == "m":
            mem_limits_mb.append(val)
        elif unit == "k":
            mem_limits_mb.append(val / 1024.0)
    assert len(mem_limits_mb) >= 5, f"Expected mem_limit for all services in docker-compose.yml, got {mem_limits_mb}"
    assert sum(mem_limits_mb) <= 12288.0, f"Total compose mem_limit {sum(mem_limits_mb)}MB exceeds 12GB (12288MB)"

    nginx_txt = nginx_file.read_text(encoding="utf-8")
    assert "2048m" in nginx_txt or "2g" in nginx_txt.lower()
    assert "proxy_buffering off" in nginx_txt
    assert "proxy_request_buffering off" in nginx_txt

    alembic_txt = alembic_file.read_text(encoding="utf-8")
    assert "pg_trgm" in alembic_txt

    if backend_dockerfile.exists() or require_app:
        assert backend_dockerfile.is_file(), f"Missing {backend_dockerfile}"
        bdf_txt = backend_dockerfile.read_text(encoding="utf-8")
        assert "dump_syms" in bdf_txt
        assert "minidump-stackwalk" in bdf_txt

    if frontend_dockerfile.exists() or require_app:
        assert frontend_dockerfile.is_file(), f"Missing {frontend_dockerfile}"
        fdf_txt = frontend_dockerfile.read_text(encoding="utf-8")
        assert "npm run build" in fdf_txt and "nginx" in fdf_txt.lower()

    if pyproject_file.exists() or require_app:
        assert pyproject_file.is_file(), f"Missing {pyproject_file}"
        pp_txt = pyproject_file.read_text(encoding="utf-8")
        assert 'requires-python = ">=3.12"' in pp_txt
        for dep in ("fastapi", "sqlalchemy", "zstandard", "arq"):
            assert dep in pp_txt, f"Missing dependency {dep} in backend/pyproject.toml"

    if config_file.exists() or require_app:
        assert config_file.is_file(), f"Missing {config_file}"
        cfg_tree = ast.parse(config_file.read_text(encoding="utf-8"))
        settings_fields: set[str] = set()
        for node in cfg_tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "Settings":
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        settings_fields.add(stmt.target.id)
        for key in required_env_keys:
            assert key in settings_fields, f"Missing Settings field {key} in backend/app/config.py"


@pytest.mark.frontend
@pytest.mark.slow
def test_t1_56_frontend_build_and_deliverables_presence() -> None:
    pkg_json = REPO_ROOT / "frontend" / "package.json"
    require_app = os.environ.get("CRASHKILLER_E2E_REQUIRE_APP", "").strip() == "1"
    if not pkg_json.exists():
        msg = "frontend/package.json not yet implemented"
        if require_app:
            pytest.fail(msg)
        pytest.skip(msg)

    pkg_text = pkg_json.read_text(encoding="utf-8")
    assert "typecheck" in pkg_text and "build" in pkg_text

    required_pages = [
        "DashboardPage.tsx", "IssuesPage.tsx", "IssueDetailPage.tsx",
        "CrashesPage.tsx", "CrashDetailPage.tsx", "SymbolsPage.tsx", "SettingsPage.tsx",
    ]
    for page in required_pages:
        assert (REPO_ROOT / "frontend" / "src" / "pages" / page).is_file(), f"Missing page {page}"

    required_components = [
        "StackTraceViewer.tsx", "VirtualLogViewer.tsx", "AgentTracePanel.tsx", "UploadModal.tsx",
    ]
    for comp in required_components:
        assert (REPO_ROOT / "frontend" / "src" / "components" / comp).is_file(), f"Missing component {comp}"

    tc = subprocess.run(["npm", "run", "typecheck"], cwd=str(REPO_ROOT / "frontend"), capture_output=True, text=True, check=False)
    assert tc.returncode == 0, f"npm run typecheck failed:\n{tc.stdout}\n{tc.stderr}"

    bd = subprocess.run(["npm", "run", "build"], cwd=str(REPO_ROOT / "frontend"), capture_output=True, text=True, check=False)
    assert bd.returncode == 0, f"npm run build failed:\n{bd.stdout}\n{bd.stderr}"

    if require_app or (REPO_ROOT / "backend" / "app" / "main.py").exists():
        for extra in (
            REPO_ROOT / "cli" / "cpp_uploader_example" / "CMakeLists.txt",
            REPO_ROOT / "cli" / "cpp_uploader_example" / "main.cpp",
            REPO_ROOT / "cli" / "cpp_uploader_example" / "README.md",
            REPO_ROOT / "backend" / "scripts" / "seed_demo_data.py",
            REPO_ROOT / "README.md",
        ):
            assert extra.is_file(), f"Missing deliverable {extra}"


async def test_t1_57_seed_demo_data_idempotency(
    client: Any,
    isolated_env: dict[str, Any],
) -> None:
    seed_script = REPO_ROOT / "backend" / "scripts" / "seed_demo_data.py"
    require_app = os.environ.get("CRASHKILLER_E2E_REQUIRE_APP", "").strip() == "1"
    if not seed_script.exists():
        msg = "backend/scripts/seed_demo_data.py not yet implemented"
        if require_app:
            pytest.fail(msg)
        pytest.skip(msg)

    import sys

    snapshots: list[tuple[int, int, int]] = []
    for run_no in (1, 2):
        proc = subprocess.run(
            [sys.executable, str(seed_script)],
            env=isolated_env["proc_env"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, f"seed_demo_data run {run_no} failed:\n{proc.stdout}\n{proc.stderr}"

        projs = (await client.get("/api/v1/projects")).json()
        demo_projs = [p for p in projs if p["key"] == "demo-game"]
        assert len(demo_projs) == 1
        pid = demo_projs[0]["id"]

        dash = (await client.get("/api/v1/dashboard", params={"project_id": pid})).json()
        issues = (await client.get("/api/v1/issues", params={"project_id": pid})).json()
        crashes = (await client.get("/api/v1/crashes", params={"project_id": pid})).json()
        assert dash["kpis"]["total_crashes"] >= 1
        assert issues["total"] >= 1
        assert crashes["total"] == dash["kpis"]["total_crashes"]
        snapshots.append((len(demo_projs), issues["total"], crashes["total"]))

    assert snapshots[1] == snapshots[0], f"seed_demo_data.py is not idempotent: run1={snapshots[0]} run2={snapshots[1]}"
