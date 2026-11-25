"""API integration tests for private and public symbol uploads, SymSrv, and deletion."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests._api_integration_helpers import _drain, open_api_harness


@pytest.fixture
async def api_harness(tmp_path: Path):
    async with open_api_harness(tmp_path) as harness:
        yield harness


async def test_symbol_upload_dedup_symsrv_and_refcounted_delete(api_harness: Any) -> None:
    client: httpx.AsyncClient = api_harness.client

    assert (await client.get("/symsrv/pingme.txt")).text == ""
    assert (await client.get("/symsrv/index2.txt")).status_code == 404

    p1 = (await client.post("/api/v1/projects", json={"key": "p1", "name": "P1"})).json()
    p2 = (await client.post("/api/v1/projects", json={"key": "p2", "name": "P2"})).json()

    pdb_bytes = b"RAW_CORE_PDB_BYTES_001"
    pe_bytes = b"RAW_CORE_PE_BYTES_001"

    # First upload PDB-only (has_pe_unwind=False)
    r1 = await client.post(
        "/api/v1/symbols/upload",
        headers={"X-API-Key": p1["api_key"]},
        data={"project_key": p1["key"], "app_version": "1.0.0"},
        files={"pdb_file": ("Core.pdb", pdb_bytes)},
    )
    assert r1.status_code == 201
    s1 = r1.json()
    assert s1["has_pe_unwind"] is False
    assert s1["deduplicated"] is False

    # Re-upload same PDB with PE -> upgrades has_pe_unwind=True and deduplicated=True
    r2 = await client.post(
        "/api/v1/symbols/upload",
        headers={"X-API-Key": p1["api_key"]},
        data={"project_key": p1["key"], "app_version": "1.0.0"},
        files={"pdb_file": ("Core.pdb", pdb_bytes), "pe_file": ("Core.dll", pe_bytes)},
    )
    assert r2.status_code == 201
    s2 = r2.json()
    assert s2["id"] == s1["id"]
    assert s2["has_pe_unwind"] is True
    assert s2["deduplicated"] is True

    did = s2["debug_id"]
    head_resp = await client.head(f"/symsrv/Core.pdb/{did}/Core.pdb")
    assert head_resp.status_code == 200
    assert head_resp.content == b""

    get_resp = await client.get(f"/symsrv/Core.pdb/{did}/Core.pdb")
    assert get_resp.status_code == 200
    assert get_resp.content == pdb_bytes

    # Upload identical symbol to project 2 (isolated per-project .sym path, shared raw archive)
    r_p2 = await client.post(
        "/api/v1/symbols/upload",
        headers={"X-API-Key": p2["api_key"]},
        data={"project_key": p2["key"], "app_version": "1.0.0"},
        files={"pdb_file": ("Core.pdb", pdb_bytes), "pe_file": ("Core.dll", pe_bytes)},
    )
    assert r_p2.status_code == 201
    sym_file_p1 = api_harness.data_dir / s2["sym_path"]
    sym_file_p2 = api_harness.data_dir / r_p2.json()["sym_path"]
    assert s2["sym_path"] != r_p2.json()["sym_path"]
    assert sym_file_p1.is_file()
    assert sym_file_p2.is_file()

    # Deleting p1's symbol row removes p1's .sym while preserving p2's .sym & shared raw archive
    assert (await client.delete(f"/api/v1/symbols/{s2['id']}")).status_code == 204
    assert not sym_file_p1.exists()
    assert sym_file_p2.is_file()
    assert (await client.get(f"/symsrv/Core.pdb/{did}/Core.pdb")).status_code == 200

    # Deleting p2's symbol row removes the last reference -> .sym & raw archive unlinked
    assert (await client.delete(f"/api/v1/symbols/{r_p2.json()['id']}")).status_code == 204
    assert not sym_file_p2.exists()
    assert (await client.get(f"/symsrv/Core.pdb/{did}/Core.pdb")).status_code == 404


async def test_public_symbol_store_upload_sync_missing_badges_and_delete(
    api_harness: Any,
) -> None:
    client: httpx.AsyncClient = api_harness.client

    proj = (
        await client.post(
            "/api/v1/projects",
            json={"key": "pub-demo", "name": "Public Demo"},
        )
    ).json()

    # 1. Manual public upload WITHOUT project_id, app_version, or project API key
    r_pub = await client.post(
        "/api/v1/symbols/public/upload",
        data={"auto_resymbolicate": "false"},
        files={"pdb_file": ("GraphicsCapture.pdb", b"RAW_GC_PDB_DATA")},
    )
    assert r_pub.status_code == 201
    pub_sym = r_pub.json()
    assert pub_sym["project_id"] is None
    assert pub_sym["app_version"] is None
    assert pub_sym["scope"] == "public"
    assert pub_sym["source"] == "upload"
    assert pub_sym["has_pe_unwind"] is False

    # Upgrade the public symbol with PE companion
    r_pub_up = await client.post(
        "/api/v1/symbols/public/upload",
        data={"auto_resymbolicate": "false"},
        files={
            "pdb_file": ("GraphicsCapture.pdb", b"RAW_GC_PDB_DATA"),
            "pe_file": ("GraphicsCapture.dll", b"RAW_GC_PE_DATA"),
        },
    )
    assert r_pub_up.status_code == 201
    up_sym = r_pub_up.json()
    assert up_sym["id"] == pub_sym["id"]
    assert up_sym["deduplicated"] is True
    assert up_sym["upgraded"] is True
    assert up_sym["has_pe_unwind"] is True

    # Direct .sym upload to public store
    direct_sym_did = "ABCDEF1234567890ABCDEF12345678901"
    direct_sym_bytes = (
        f"MODULE windows x86_64 {direct_sym_did} DirectHelper.pdb\n"
        "STACK CFI INIT 1000 20 .cfa: $rsp 8 +\n"
        "PUBLIC 1000 0 DirectFunc\n"
    ).encode()
    r_sym = await client.post(
        "/api/v1/symbols/public/upload",
        files={"pdb_file": ("DirectHelper.sym", direct_sym_bytes)},
    )
    assert r_sym.status_code == 201
    assert r_sym.json()["debug_id"] == direct_sym_did
    assert r_sym.json()["scope"] == "public"

    # Project inventory remains isolated from public symbol store
    proj_list = (
        await client.get("/api/v1/symbols", params={"scope": "project"})
    ).json()
    assert proj_list["total"] == 0

    pub_list = (
        await client.get("/api/v1/symbols", params={"scope": "public"})
    ).json()
    assert pub_list["total"] == 2
    assert all(item["project_id"] is None for item in pub_list["items"])

    # WinDbg /symsrv serves raw archived public symbol
    gc_did = up_sym["debug_id"]
    assert (
        await client.get(f"/symsrv/GraphicsCapture.pdb/{gc_did}/GraphicsCapture.pdb")
    ).status_code == 200

    # 2. Seed a crash with missing GraphicsCapture.pdb (Windows system module) & test MSDL sync
    sync_did = "90D28435EAAB109449C47402C53C42D31"
    from app.db.models import CrashReport

    async with api_harness.app.state.session_factory() as session:
        crash_id = uuid.uuid4()
        dmp_src = api_harness.data_dir / "tmp_sync.dmp"
        dmp_src.write_bytes(b"MDMP_TEST")
        dmp_rel, _, _ = api_harness.app.state.storage.compress_dump(dmp_src, crash_id)
        dmp_src.unlink(missing_ok=True)
        session.add(
            CrashReport(
                id=crash_id,
                project_id=uuid.UUID(proj["id"]),
                status="ANALYZED",
                app_version="1.0.0",
                dump_path=dmp_rel,
                missing_symbols_json=[
                    {
                        "module_name": "GraphicsCapture.dll",
                        "debug_file": "GraphicsCapture.pdb",
                        "debug_id": sync_did,
                        "code_file": r"C:\Windows\System32\GraphicsCapture.dll",
                        "code_id": "65A1B2C31a000",
                    }
                ],
            )
        )
        await session.commit()

    missing_before = (await client.get("/api/v1/symbols/missing")).json()
    gc_missing = next(m for m in missing_before if m["debug_id"] == sync_did)
    assert gc_missing["is_windows_system"] is True
    assert gc_missing["is_available_locally"] is False

    # Wire mock HTTP transport + dump_syms returning exact sync_did for GraphicsCapture.pdb
    fake_pdb = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\x00\x00\x00" + (b"\x00" * 128)
    fake_pe = b"MZ" + (b"\x00" * 128)

    def _msdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/graphicscapture.pdb/{sync_did}/graphicscapture.pdb"):
            return httpx.Response(200, content=fake_pdb)
        if request.url.path.endswith("/graphicscapture.dll/65A1B2C31a000/graphicscapture.dll"):
            return httpx.Response(200, content=fake_pe)
        return httpx.Response(404)

    orig_exec = api_harness.app.state.exec_fn

    async def _sync_exec(*cmd: str, **kwargs: Any) -> Any:
        if "dump_syms" in Path(cmd[0]).name and any(
            "graphicscapture" in str(a).lower() for a in cmd[1:]
        ):
            has_pe = len(cmd) >= 3
            lines = [
                f"MODULE windows x86_64 {sync_did} GraphicsCapture.pdb",
                "INFO CODE_ID 65A1B2C31A000 GraphicsCapture.dll",
                "PUBLIC 1000 0 CaptureStart",
            ]
            if has_pe:
                lines.append("STACK CFI INIT 1000 20 .cfa: $rsp 8 +")
            out = ("\n".join(lines) + "\n").encode()

            class _P:
                returncode = 0

                async def communicate(self) -> tuple[bytes, bytes]:
                    return out, b""

                def kill(self) -> None:
                    return None

            return _P()
        return await orig_exec(*cmd, **kwargs)

    api_harness.app.state.http_transport = httpx.MockTransport(_msdl_handler)
    api_harness.app.state.exec_fn = _sync_exec
    try:
        r_sync = await client.post(
            "/api/v1/symbols/public/sync",
            json={
                "mode": "targets",
                "targets": [
                    {
                        "module_name": "GraphicsCapture.dll",
                        "debug_id": sync_did,
                        "code_id": "65A1B2C31a000",
                    }
                ],
                "force": True,
                "auto_resymbolicate": True,
            },
        )
    finally:
        api_harness.app.state.exec_fn = orig_exec
        api_harness.app.state.http_transport = None

    assert r_sync.status_code == 200
    sync_body = r_sync.json()
    assert sync_body["imported"] == 1
    assert sync_body["results"][0]["status"] == "imported"
    assert sync_body["results"][0]["has_pe_unwind"] is True
    assert sync_body["resymbolicate_queued_count"] == 1
    await _drain(api_harness)

    # Confirm synced symbol appears in public store with source="msdl" and deletes cleanly
    msdl_list = (
        await client.get(
            "/api/v1/symbols", params={"scope": "public", "source": "msdl"}
        )
    ).json()
    assert msdl_list["total"] == 1
    msdl_sym = msdl_list["items"][0]
    assert msdl_sym["debug_id"] == sync_did
    assert msdl_sym["project_id"] is None
    assert msdl_sym["app_version"] is None
    sym_on_disk = api_harness.data_dir / msdl_sym["sym_path"]
    assert sym_on_disk.is_file()

    assert (await client.delete(f"/api/v1/symbols/{msdl_sym['id']}")).status_code == 204
    assert not sym_on_disk.exists()
