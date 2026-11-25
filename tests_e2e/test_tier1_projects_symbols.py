from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.tier1


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
    pdb1 = fake_toolchain.make_pdb(
        filename="Alpha.pdb", debug_id="111111111111111111111111111111111"
    )
    pe1 = fake_toolchain.make_pe(
        filename="Alpha.exe", debug_file="Alpha.pdb", debug_id="111111111111111111111111111111111"
    )
    pdb2 = fake_toolchain.make_pdb(
        filename="Beta.pdb", debug_id="222222222222222222222222222222221"
    )

    r1 = await helpers.upload_symbol(
        client,
        proj["key"],
        proj["api_key"],
        pdb1,
        "Alpha.pdb",
        pe1,
        "Alpha.exe",
        app_version="1.0.0",
    )
    r2 = await helpers.upload_symbol(
        client, proj["key"], proj["api_key"], pdb2, "Beta.pdb", None, app_version="2.0.0"
    )
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
                    {
                        "module": "MissingMod.dll",
                        "function": "HiddenFunc()",
                        "file": "src/m.cpp",
                        "line": 12,
                    }
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
                    {
                        "module": "GameCore.dll",
                        "function": "GameCore::CrashHere()",
                        "file": "src/core.cpp",
                        "line": 77,
                    }
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
    sym_up = await helpers.upload_symbol(
        client, proj["key"], proj["api_key"], pdb, "GameCore.pdb", pe, "GameCore.dll"
    )
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
    pdb = fake_toolchain.make_pdb(
        filename="DelMod.pdb", debug_id="999911112222333344445555666677771"
    )
    up = await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb, "DelMod.pdb")
    assert up.status_code == 201
    sym_id = up.json()["id"]

    del_resp = await client.delete(f"/api/v1/symbols/{sym_id}")
    assert del_resp.status_code == 204
    assert del_resp.content == b""

    list_resp = await client.get("/api/v1/symbols", params={"project_id": proj["id"]})
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 0
