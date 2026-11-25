"""API integration tests for Windows missing symbol sync SSE streams and cancellation."""

from __future__ import annotations

import json
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


async def test_full_missing_windows_sync_stream_skips_complete_cache_and_resym_status(
    api_harness: Any,
) -> None:
    from app.db.models import CrashReport

    client: httpx.AsyncClient = api_harness.client
    proj = (
        await client.post(
            "/api/v1/projects",
            json={"key": "full-sync-demo", "name": "Full Sync Demo"},
        )
    ).json()

    # Build 28 missing Windows modules (exceeding the legacy 20-item cap):
    # - 3 already cached locally with PE unwind (must not burn sync slots)
    # - 25 uncached Windows modules (including msvcp_win.dll, kernel.appcore.dll, InputHost.pdb)
    win_specs: list[tuple[str, str, str]] = [
        ("ntdll.dll", "ntdll.pdb", f"{1:032X}1"),
        ("kernel32.dll", "kernel32.pdb", f"{2:032X}1"),
        ("KERNELBASE.dll", "kernelbase.pdb", f"{3:032X}1"),
        ("msvcp_win.dll", "msvcp_win.pdb", f"{4:032X}1"),
        ("kernel.appcore.dll", "Kernel.Appcore.pdb", f"{5:032X}1"),
        ("inputhost.dll", "InputHost.pdb", f"{6:032X}1"),
        ("CoreMessaging.dll", "CoreMessaging.pdb", f"{7:032X}1"),
        ("WinTypes.dll", "WinTypes.pdb", f"{8:032X}1"),
        ("secur32.dll", "secur32.pdb", f"{9:032X}1"),
        ("wintrust.dll", "wintrust.pdb", f"{10:032X}1"),
        ("d3d11.dll", "d3d11.pdb", f"{11:032X}1"),
        ("dxgi.dll", "dxgi.pdb", f"{12:032X}1"),
        ("dcomp.dll", "dcomp.pdb", f"{13:032X}1"),
        ("user32.dll", "user32.pdb", f"{14:032X}1"),
        ("gdi32.dll", "gdi32.pdb", f"{15:032X}1"),
        ("win32u.dll", "win32u.pdb", f"{16:032X}1"),
        ("ws2_32.dll", "ws2_32.pdb", f"{17:032X}1"),
        ("mswsock.dll", "mswsock.pdb", f"{18:032X}1"),
        ("winmm.dll", "winmm.pdb", f"{19:032X}1"),
        ("ucrtbase.dll", "ucrtbase.pdb", f"{20:032X}1"),
        ("combase.dll", "combase.pdb", f"{21:032X}1"),
        ("ole32.dll", "ole32.pdb", f"{22:032X}1"),
        ("rpcrt4.dll", "rpcrt4.pdb", f"{23:032X}1"),
        ("advapi32.dll", "advapi32.pdb", f"{24:032X}1"),
        ("bcrypt.dll", "bcrypt.pdb", f"{25:032X}1"),
        ("crypt32.dll", "crypt32.pdb", f"{26:032X}1"),
        ("shell32.dll", "shell32.pdb", f"{27:032X}1"),
        ("shlwapi.dll", "shlwapi.pdb", f"{28:032X}1"),
    ]

    # Pre-cache first 3 with complete PE unwind
    for mod_name, pdb_name, did in win_specs[:3]:
        sym_content = (
            f"MODULE windows x86_64 {did} {pdb_name}\n"
            f"INFO CODE_ID 65A1B2C31A000 {mod_name}\n"
            "STACK CFI INIT 1000 20 .cfa: $rsp 8 +\n"
            "PUBLIC 1000 0 PreCachedFn\n"
        ).encode()
        stem = Path(pdb_name).stem
        api_harness.app.state.storage.atomic_write_bytes(
            f"symbols/public_cache/{pdb_name}/{did}/{stem}.sym",
            sym_content,
        )

    third_party_did = f"{99:032X}1"
    missing_entries = [
        {
            "module_name": mod,
            "debug_file": pdb,
            "debug_id": did,
            "code_file": mod,
            "code_id": "65A1B2C31a000",
        }
        for mod, pdb, did in win_specs
    ] + [
        {
            # Stripped binary with empty debug_file and all-zero debug_id must be filtered out
            "module_name": "swscale-8.dll",
            "debug_file": "",
            "debug_id": "000000000000000000000000000000000",
            "code_file": "swscale-8.dll",
            "code_id": "65F46B2DAE000",
        },
        {
            "module_name": "MyCustomPlugin.dll",
            "debug_file": "MyCustomPlugin.pdb",
            "debug_id": third_party_did,
            "code_file": "MyCustomPlugin.dll",
            "code_id": "65A1B2C31a000",
        },
    ]

    crash_id = uuid.uuid4()
    async with api_harness.app.state.session_factory() as session:
        dmp_src = api_harness.data_dir / "full_sync.dmp"
        dmp_src.write_bytes(b"MDMP_FULL_SYNC")
        dmp_rel, _, _ = api_harness.app.state.storage.compress_dump(dmp_src, crash_id)
        dmp_src.unlink(missing_ok=True)
        session.add(
            CrashReport(
                id=crash_id,
                project_id=uuid.UUID(proj["id"]),
                status="ANALYZED",
                app_version="1.0.0",
                dump_path=dmp_rel,
                missing_symbols_json=missing_entries,
            )
        )
        await session.commit()

    # Missing queue filters out zeroed swscale-8.dll and exposes X-Total-Count header
    miss_resp = await client.get("/api/v1/symbols/missing", params={"project_id": proj["id"]})
    assert miss_resp.status_code == 200
    assert miss_resp.headers.get("x-total-count") == "29"
    miss_items = miss_resp.json()
    assert not any(it["module_name"] == "swscale-8.dll" for it in miss_items)
    assert all(
        it["is_windows_system"] is True
        for it in miss_items
        if it["debug_id"] != third_party_did
    )

    fake_pdb = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\x00\x00\x00" + (b"\x00" * 128)
    did_by_pdb_low = {pdb.lower(): (pdb, did) for _, pdb, did in win_specs}

    def _msdl_handler(request: httpx.Request) -> httpx.Response:
        parts = [p for p in request.url.path.split("/") if p]
        if len(parts) >= 3 and parts[-1].lower().endswith(".pdb"):
            return httpx.Response(200, content=fake_pdb)
        return httpx.Response(404)

    orig_exec = api_harness.app.state.exec_fn
    orig_disp_exec = api_harness.app.state.dispatcher.exec_fn
    pub_cache_root = api_harness.app.state.storage.partition_path("symbols/public_cache")

    async def _full_exec(*cmd: str, **kwargs: Any) -> Any:
        bin_name = Path(cmd[0]).name
        if "dump_syms" in bin_name:
            target_pdb = Path(cmd[-1]).name.lower()
            pdb_name, did = did_by_pdb_low[target_pdb]
            out = (
                f"MODULE windows x86_64 {did} {pdb_name}\n"
                "STACK CFI INIT 1000 20 .cfa: $rsp 8 +\n"
                "PUBLIC 1000 0 ExportedFn\n"
            ).encode()
        else:
            # Simulate minidump-stackwalk reading public_cache & omitting unreferenced modules
            modules_out = []
            for mod, pdb, did in win_specs:
                stem = Path(pdb).stem
                has_sym = (pub_cache_root / pdb / did / f"{stem}.sym").is_file()
                modules_out.append(
                    {
                        "filename": mod,
                        "debug_file": pdb,
                        "debug_id": did,
                        "code_file": mod,
                        "code_id": "65A1B2C31a000",
                        "loaded_symbols": has_sym,
                        "missing_symbols": not has_sym,
                    }
                )
            modules_out.append(
                {
                    "filename": "MyCustomPlugin.dll",
                    "debug_file": "MyCustomPlugin.pdb",
                    "debug_id": third_party_did,
                    "loaded_symbols": False,
                    "missing_symbols": True,
                }
            )
            modules_out.append(
                {
                    # Unreferenced module (not on stack) must not re-enter missing queue
                    "filename": "UnreferencedWin.dll",
                    "debug_file": "UnreferencedWin.pdb",
                    "debug_id": f"{88:032X}1",
                    "loaded_symbols": False,
                    "missing_symbols": False,
                }
            )
            out = json.dumps(
                {
                    "crash_info": {"type": "EXCEPTION_ACCESS_VIOLATION_READ", "address": "0x0"},
                    "system_info": {"os": "Windows NT", "os_ver": "10.0.19041", "cpu": "amd64"},
                    "threads": [
                        {
                            "thread_id": 1,
                            "frames": [{"frame": 0, "offset": "0x1000", "module": "ntdll.dll"}],
                        }
                    ],
                    "modules": modules_out,
                }
            ).encode()

        class _P:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return out, b""

            def kill(self) -> None:
                return None

        return _P()

    api_harness.app.state.http_transport = httpx.MockTransport(_msdl_handler)
    api_harness.app.state.exec_fn = _full_exec
    api_harness.app.state.dispatcher.exec_fn = _full_exec
    try:
        stream_resp = await client.post(
            "/api/v1/symbols/public/sync/stream",
            json={
                "mode": "missing_windows",
                "project_id": proj["id"],
                "force": True,
                "auto_resymbolicate": True,
            },
        )
        assert stream_resp.status_code == 200
        assert stream_resp.headers["content-type"].startswith("text/event-stream")

        events: list[dict[str, Any]] = []
        for block in stream_resp.text.split("\n\n"):
            for line in block.splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

        assert events[0]["type"] == "start"
        assert events[0]["total"] == 25
        assert events[0]["already_cached"] == 3
        progress_events = [e for e in events if e["type"] == "progress"]
        assert len(progress_events) == 25
        assert [e["completed"] for e in progress_events] == list(range(1, 26))

        complete_evt = events[-1]
        assert complete_evt["type"] == "complete"
        summary = complete_evt["summary"]
        assert summary["imported"] == 25
        assert summary["cached_hit"] == 3
        assert summary["resymbolicate_queued_count"] == 1
        assert summary["resymbolicate_crash_ids"] == [str(crash_id)]

        await _drain(api_harness)
    finally:
        api_harness.app.state.exec_fn = orig_exec
        api_harness.app.state.dispatcher.exec_fn = orig_disp_exec
        api_harness.app.state.http_transport = None

    # After auto-resymbolication completes, missing queue shrunk from 29 to ONLY MyCustomPlugin.dll!
    miss_after = (
        await client.get("/api/v1/symbols/missing", params={"project_id": proj["id"]})
    ).json()
    assert len(miss_after) == 1
    assert miss_after[0]["debug_id"] == third_party_did

    # Batch resymbolicate status endpoint reports completed crash and validates bad UUIDs
    st_resp = await client.get(
        "/api/v1/symbols/resymbolicate/status",
        params={"crash_ids": f"{crash_id},{crash_id}"},
    )
    assert st_resp.status_code == 200
    st_body = st_resp.json()
    assert st_body["total"] == 1
    assert st_body["analyzed"] == 1
    assert st_body["in_progress"] == 0
    assert st_body["done"] is True

    bad_st = await client.get(
        "/api/v1/symbols/resymbolicate/status",
        params={"crash_ids": "not-a-uuid"},
    )
    assert bad_st.status_code == 422

    # Second stream sync with 0 remaining Windows symbols emits start(total=0) + complete cleanly
    empty_stream = await client.post(
        "/api/v1/symbols/public/sync/stream",
        json={"mode": "missing_windows", "project_id": proj["id"]},
    )
    assert empty_stream.status_code == 200
    empty_events = [
        json.loads(line[6:])
        for block in empty_stream.text.split("\n\n")
        for line in block.splitlines()
        if line.startswith("data: ")
    ]
    assert [e["type"] for e in empty_events] == ["start", "complete"]
    assert empty_events[0]["total"] == 0
    assert empty_events[1]["summary"]["imported"] == 0


async def test_sync_stream_reports_triaging_active_crashes_all_projects_scope_and_path_df(
    api_harness: Any,
) -> None:
    from app.db.models import CrashReport

    client: httpx.AsyncClient = api_harness.client
    proj = (
        await client.post(
            "/api/v1/projects",
            json={"key": "busy-scope-demo", "name": "Busy Scope Demo"},
        )
    ).json()

    proj_did = "B0B0B0B0B0B0B0B0B0B0B0B0B0B0B0B11"
    win_did = "C0C0C0C0C0C0C0C0C0C0C0C0C0C0C0C11"

    # Put project-scoped symbol on disk so project_id=None ("All Projects") detects it
    api_harness.app.state.storage.atomic_write_bytes(
        f"symbols/sym/{proj['id']}/AppCore.pdb/{proj_did}/AppCore.sym",
        f"MODULE windows x86_64 {proj_did} AppCore.pdb\nPUBLIC 1000 0 AppMain\n".encode(),
    )

    triaging_crash_id = uuid.uuid4()
    async with api_harness.app.state.session_factory() as session:
        dmp_src = api_harness.data_dir / "triaging.dmp"
        dmp_src.write_bytes(b"MDMP_TRIAGING")
        dmp_rel, _, _ = api_harness.app.state.storage.compress_dump(dmp_src, triaging_crash_id)
        dmp_src.unlink(missing_ok=True)
        session.add(
            CrashReport(
                id=triaging_crash_id,
                project_id=uuid.UUID(proj["id"]),
                status="TRIAGING",
                app_version="1.0.0",
                dump_path=dmp_rel,
                missing_symbols_json=[
                    {
                        "module_name": "AppCore.dll",
                        "debug_file": "AppCore.pdb",
                        "debug_id": proj_did,
                    },
                    {
                        # Path-prefixed Windows PDB must normalize and sync in missing_windows mode
                        "module_name": "ntdll.dll",
                        "debug_file": r"C:\Windows\System32\ntdll.pdb",
                        "debug_id": win_did,
                        "code_file": "ntdll.dll",
                    },
                ],
            )
        )
        await session.commit()

    # All-projects missing queue detects project-scoped AppCore.pdb as locally available
    # and normalizes C:\Windows\System32\ntdll.pdb to ntdll.pdb.
    all_missing = (await client.get("/api/v1/symbols/missing")).json()
    app_entry = next(it for it in all_missing if it["debug_id"] == proj_did)
    assert app_entry["is_available_locally"] is True
    assert app_entry["local_scope"] == "project"
    win_entry = next(it for it in all_missing if it["debug_id"] == win_did)
    assert win_entry["debug_file"] == "ntdll.pdb"
    assert win_entry["is_windows_system"] is True

    fake_pdb = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\x00\x00\x00" + (b"\x00" * 128)
    orig_exec = api_harness.app.state.exec_fn

    async def _dump_exec(*_cmd: str, **_kwargs: Any) -> Any:
        out = (
            f"MODULE windows x86_64 {win_did} ntdll.pdb\n"
            "STACK CFI INIT 1000 20 .cfa: $rsp 8 +\n"
            "PUBLIC 1000 0 KiUserExceptionDispatcher\n"
        ).encode()

        class _Proc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return out, b""

            def kill(self) -> None:
                pass

        return _Proc()

    api_harness.app.state.exec_fn = _dump_exec
    api_harness.app.state.http_transport = httpx.MockTransport(
        lambda req: (
            httpx.Response(200, content=fake_pdb)
            if req.url.path.endswith(f"/ntdll.pdb/{win_did}/ntdll.pdb")
            else httpx.Response(404)
        )
    )
    try:
        resp = await client.post(
            "/api/v1/symbols/public/sync/stream",
            json={
                "mode": "missing_windows",
                "project_id": proj["id"],
                "force": True,
                "auto_resymbolicate": True,
            },
        )
        assert resp.status_code == 200
        events = [
            json.loads(line[6:])
            for block in resp.text.split("\n\n")
            for line in block.splitlines()
            if line.startswith("data: ")
        ]
        summary = events[-1]["summary"]
        assert summary["imported"] == 1
        # Crash is TRIAGING => must report skipped_active_count=1 and exact active crash_id
        assert summary["resymbolicate_queued_count"] == 0
        assert summary["resymbolicate_crash_ids"] == []
        assert summary["resymbolicate_skipped_active_count"] == 1
        assert summary["resymbolicate_active_crash_ids"] == [str(triaging_crash_id)]
    finally:
        api_harness.app.state.exec_fn = orig_exec
        api_harness.app.state.http_transport = None


async def test_sync_stream_client_disconnect_cancels_server_task(
    api_harness: Any,
) -> None:
    """Closing the SSE stream mid-batch cancels in-flight MSDL fetches promptly."""
    import asyncio

    client: httpx.AsyncClient = api_harness.client
    started_event = asyncio.Event()
    cancelled_event = asyncio.Event()

    async def slow_handler(_request: httpx.Request) -> httpx.Response:
        started_event.set()
        try:
            await asyncio.sleep(30.0)
        except asyncio.CancelledError:
            cancelled_event.set()
            raise
        return httpx.Response(404)

    api_harness.app.state.http_transport = httpx.MockTransport(slow_handler)
    try:
        async with client.stream(
            "POST",
            "/api/v1/symbols/public/sync/stream",
            json={
                "mode": "targets",
                "targets": [
                    {
                        "module_name": "ntdll.dll",
                        "debug_file": "ntdll.pdb",
                        "debug_id": "23E72AA7E3873AC79882BF6E394DA71E1",
                    }
                ],
                "force": True,
                "auto_resymbolicate": False,
            },
        ) as resp:
            assert resp.status_code == 200
            line_iter = resp.aiter_lines()
            seen_data: list[dict[str, Any]] = []
            async for line in line_iter:
                if line.startswith("data: "):
                    evt = json.loads(line[6:])
                    seen_data.append(evt)
                    if evt["type"] == "item_start":
                        break
            assert any(e["type"] == "start" for e in seen_data)
            assert any(e["type"] == "item_start" for e in seen_data)
            await asyncio.wait_for(started_event.wait(), timeout=5.0)
        # Exiting the stream context simulates client AbortController.abort()
        await asyncio.wait_for(cancelled_event.wait(), timeout=5.0)
        assert cancelled_event.is_set()
    finally:
        api_harness.app.state.http_transport = None


async def test_sync_skips_invalid_zero_guid_in_missing_windows_and_rejects_in_targets(
    api_harness: Any,
) -> None:
    """Invalid all-zero GUID PDB rows skip missing_windows and fail with 422 in targets."""
    from app.db.models import CrashReport

    client: httpx.AsyncClient = api_harness.client
    proj = (
        await client.post(
            "/api/v1/projects",
            json={"key": "zero-guid-sync", "name": "Zero GUID Sync"},
        )
    ).json()

    valid_did = "D1D2D3D4D5D6D7D8D9DADBDCDDDEDFD11"
    zero_age_did = "0" * 32 + "1"
    crash_id = uuid.uuid4()
    async with api_harness.app.state.session_factory() as session:
        session.add(
            CrashReport(
                id=crash_id,
                project_id=uuid.UUID(proj["id"]),
                status="ANALYZED",
                app_version="1.0.0",
                missing_symbols_json=[
                    {
                        "module_name": "kernel32.dll",
                        "debug_file": "kernel32.pdb",
                        "debug_id": zero_age_did,
                        "code_file": "kernel32.dll",
                        "code_id": "00000000",
                    },
                    {
                        "module_name": "ntdll.dll",
                        "debug_file": "ntdll.pdb",
                        "debug_id": valid_did,
                        "code_file": "ntdll.dll",
                        "code_id": "00000000",
                    },
                ],
            )
        )
        await session.commit()

    # mode="targets" rejects all-zero GUID with non-zero age using HTTP 422
    bad_resp = await client.post(
        "/api/v1/symbols/public/sync/stream",
        json={
            "mode": "targets",
            "targets": [
                {
                    "module_name": "kernel32.dll",
                    "debug_file": "kernel32.pdb",
                    "debug_id": zero_age_did,
                },
                {
                    "module_name": "ntdll.dll",
                    "debug_file": "ntdll.pdb",
                    "debug_id": valid_did,
                },
            ],
            "project_id": proj["id"],
            "force": True,
            "auto_resymbolicate": False,
        },
    )
    assert bad_resp.status_code == 422

    # mode="missing_windows" skips the invalid zero-GUID row and attempts only the valid row
    requested: list[str] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        requested.append(req.url.path)
        return httpx.Response(404)

    api_harness.app.state.http_transport = httpx.MockTransport(_handler)
    try:
        win_resp = await client.post(
            "/api/v1/symbols/public/sync/stream",
            json={
                "mode": "missing_windows",
                "project_id": proj["id"],
                "force": True,
                "auto_resymbolicate": False,
            },
        )
        assert win_resp.status_code == 200
        events = [
            json.loads(line[6:])
            for block in win_resp.text.split("\n\n")
            for line in block.splitlines()
            if line.startswith("data: ")
        ]
        summary = events[-1]["summary"]
        assert summary["attempted"] == 1
        assert [r["debug_id"] for r in summary["results"]] == [valid_did]
        assert all(zero_age_did not in p for p in requested)

        # Sanitized target (code_id omitted after frontend drops all-zero code_id) succeeds
        good_resp = await client.post(
            "/api/v1/symbols/public/sync/stream",
            json={
                "mode": "targets",
                "targets": [
                    {
                        "module_name": "ntdll.dll",
                        "debug_file": "ntdll.pdb",
                        "debug_id": valid_did,
                        "code_file": "ntdll.dll",
                    }
                ],
                "project_id": proj["id"],
                "force": True,
                "auto_resymbolicate": False,
            },
        )
        assert good_resp.status_code == 200
    finally:
        api_harness.app.state.http_transport = None
