"""Unit and integration tests for non-blocking Microsoft Symbol Server (MSDL) import."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.base import Base
from app.db.models import AIAnalysisRun, CrashJob, CrashReport, Project
from app.db.session import create_async_engine_from_url
from app.services.events import InMemoryEventBus
from app.services.ms_symbol_service import (
    CIRCUIT_FAILURE_THRESHOLD,
    SYMSRV_USER_AGENT,
    WindowsSymbolCandidate,
    _normalize_pe_code_ids,
    fetch_and_import_windows_symbols,
    has_cached_symbol,
    has_cached_symbol_detail,
    is_circuit_open,
    is_negative_cached,
    is_windows_system_module,
    parse_manual_ms_target,
    select_windows_system_candidates,
    split_public_symbol_urls,
)
from app.services.storage import StorageManager
from app.workers.jobs import stage_crash_job
from app.workers.tasks import fetch_ms_symbols
from app.workers.worker import TaskDispatcher
from tests.conftest import make_disk_usage_fn, make_mock_exec

NTDLL_DEBUG_ID = "7F5986E31FA943588F5488168233C87F1"
NTDLL_CODE_ID = "654321AB1F5000"
KERNELBASE_DEBUG_ID = "AABBCCDDEEFF001122334455667788991"
D3D11_DEBUG_ID = "11223344556677889900AABBCCDDEEFF2"

FAKE_PDB_BYTES = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\x00\x00\x00" + (b"\x00" * 128)
FAKE_PE_BYTES = b"MZ" + (b"\x00" * 128)
FAKE_CAB_BYTES = b"MSCF" + (b"\x00" * 128)


def _sample_sym(debug_file: str, debug_id: str, code_id: str | None = None) -> bytes:
    lines = [f"MODULE windows x86_64 {debug_id} {debug_file}"]
    if code_id:
        lines.append(f"INFO CODE_ID {code_id} {Path(debug_file).stem}.dll")
        lines.append("STACK CFI INIT 1000 40 .cfa: $rsp 8 + .ra: .cfa -8 + ^")
    lines.append("PUBLIC 1000 0 KiUserExceptionDispatcher")
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_split_public_symbol_urls() -> None:
    breakpad, symsrv = split_public_symbol_urls(
        [
            "https://symbols.mozilla.org/",
            "https://msdl.microsoft.com/download/symbols/",
            "https://msdl.microsoft.com/download/symbols",
            "symsrv+https://sym.internal.corp/store",
            "https://custom.example.com/symsrv/",
            "ftp://ignored.example.com/symbols",
            "",
        ]
    )
    assert breakpad == ["https://symbols.mozilla.org/"]
    assert symsrv == [
        "https://msdl.microsoft.com/download/symbols/",
        "https://sym.internal.corp/store",
        "https://custom.example.com/symsrv/",
    ]


def test_windows_system_module_filtering_and_privacy() -> None:
    assert is_windows_system_module(
        filename=r"C:\Windows\System32\ntdll.dll",
        code_file="ntdll.dll",
        module_name="ntdll.dll",
        debug_file="ntdll.pdb",
    )
    assert is_windows_system_module(
        filename=r"C:\Windows\SysWOW64\d3d11.dll",
        code_file=None,
        module_name="d3d11.dll",
        debug_file="d3d11.pdb",
    )
    # App-local Microsoft CRT runtime DLL should still be allowed.
    assert is_windows_system_module(
        filename=r"C:\Program Files\MyGame\vcruntime140.dll",
        code_file="vcruntime140.dll",
        module_name="vcruntime140.dll",
        debug_file="vcruntime140.amd64.pdb",
    )
    # Proprietary app binaries must never be sent to MSDL.
    assert not is_windows_system_module(
        filename=r"C:\Program Files\MyGame\MyGame.exe",
        code_file="MyGame.exe",
        module_name="MyGame.exe",
        debug_file="MyGame.pdb",
    )
    assert not is_windows_system_module(
        filename=r"D:\Workspace\SecretPlugin.dll",
        code_file="SecretPlugin.dll",
        module_name="SecretPlugin.dll",
        debug_file="SecretPlugin.pdb",
    )
    # Third-party GPU drivers inside DriverStore must not match by path alone.
    assert not is_windows_system_module(
        filename=(
            r"C:\Windows\System32\DriverStore\FileRepository"
            r"\nv_dispig.inf_amd64_1234\nvwgf2umx.dll"
        ),
        code_file="nvwgf2umx.dll",
        module_name="nvwgf2umx.dll",
        debug_file="nvwgf2umx.pdb",
    )
    # Non-PDB symbols are rejected.
    assert not is_windows_system_module(
        filename="/usr/lib/libc.so.6",
        code_file="libc.so.6",
        module_name="libc.so.6",
        debug_file="libc.so.6",
    )


def test_select_windows_system_candidates_prioritizes_crashing_thread_and_respects_cache(
    storage_manager: StorageManager,
    test_settings: Settings,
) -> None:
    project_id = uuid.uuid4()
    parsed_stacks = {
        "crashing_thread_index": 1,
        "threads": [
            {
                "thread_index": 0,
                "frames": [
                    {"module": "d3d11.dll", "missing_symbols": True},
                    {"module": "Game.exe", "missing_symbols": True},
                ],
            },
            {
                "thread_index": 1,
                "frames": [
                    {"module": "ntdll.dll", "missing_symbols": True},
                    {"module": "kernelbase.dll", "missing_symbols": True},
                    {"module": "Plugin.dll", "missing_symbols": True},
                ],
            },
        ],
        "modules": [
            {
                "filename": r"C:\Windows\System32\ntdll.dll",
                "module_name": "ntdll.dll",
                "debug_file": "ntdll.pdb",
                "debug_id": NTDLL_DEBUG_ID,
                "code_file": "ntdll.dll",
                "code_id": NTDLL_CODE_ID,
                "loaded_symbols": False,
                "missing_symbols": True,
            },
            {
                "filename": r"C:\Windows\System32\kernelbase.dll",
                "module_name": "kernelbase.dll",
                "debug_file": "kernelbase.pdb",
                "debug_id": KERNELBASE_DEBUG_ID,
                "loaded_symbols": False,
                "missing_symbols": True,
            },
            {
                "filename": r"C:\Windows\System32\d3d11.dll",
                "module_name": "d3d11.dll",
                "debug_file": "d3d11.pdb",
                "debug_id": D3D11_DEBUG_ID,
                "loaded_symbols": False,
                "missing_symbols": True,
            },
            {
                # Loaded Windows DLL not referenced by any stack frame should be skipped.
                "filename": r"C:\Windows\System32\crypt32.dll",
                "module_name": "crypt32.dll",
                "debug_file": "crypt32.pdb",
                "debug_id": "1234567890ABCDEF1234567890ABCDEF1",
                "loaded_symbols": False,
                "missing_symbols": True,
            },
            {
                "filename": r"C:\Program Files\MyGame\Plugin.dll",
                "module_name": "Plugin.dll",
                "debug_file": "Plugin.pdb",
                "debug_id": "999999999999999999999999999999991",
                "loaded_symbols": False,
                "missing_symbols": True,
            },
        ],
    }

    # Pre-cache kernelbase.sym in public_cache so it must be skipped.
    storage_manager.atomic_write_bytes(
        f"symbols/public_cache/kernelbase.pdb/{KERNELBASE_DEBUG_ID}/kernelbase.sym",
        _sample_sym("kernelbase.pdb", KERNELBASE_DEBUG_ID),
    )

    # Linux dumps always yield empty list.
    assert (
        select_windows_system_candidates(
            os_version="Linux 6.8.0",
            parsed_stacks_json=parsed_stacks,
            missing_symbols_json=None,
            storage=storage_manager,
            settings=test_settings,
            project_id=project_id,
        )
        == []
    )

    candidates = select_windows_system_candidates(
        os_version="Windows NT 10.0.22631",
        parsed_stacks_json=parsed_stacks,
        missing_symbols_json=None,
        storage=storage_manager,
        settings=test_settings,
        project_id=project_id,
    )
    assert [c.debug_file for c in candidates] == ["ntdll.pdb", "d3d11.pdb"]

    # Cap at 1 keeps only the crashing thread's first Windows module.
    capped_settings = test_settings.model_copy(update={"MS_SYMBOL_MAX_MODULES_PER_CRASH": 1})
    capped = select_windows_system_candidates(
        os_version="Windows NT 10.0.22631",
        parsed_stacks_json=parsed_stacks,
        missing_symbols_json=None,
        storage=storage_manager,
        settings=capped_settings,
        project_id=project_id,
    )
    assert [c.debug_file for c in capped] == ["ntdll.pdb"]


async def test_fetch_and_import_pdb_pe_redirect_and_cache_hit(
    storage_manager: StorageManager,
    test_settings: Settings,
) -> None:
    requested_urls: list[str] = []
    seen_user_agents: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        seen_user_agents.append(request.headers.get("user-agent", ""))
        path = request.url.path
        if path.endswith(f"/ntdll.pdb/{NTDLL_DEBUG_ID}/ntdll.pdb"):
            return httpx.Response(
                302,
                headers={"Location": "https://cdn.example.invalid/blobs/ntdll.pdb"},
            )
        if request.url.host == "cdn.example.invalid":
            return httpx.Response(200, content=FAKE_PDB_BYTES)
        if path.endswith(f"/ntdll.dll/{NTDLL_CODE_ID}/ntdll.dll"):
            return httpx.Response(200, content=FAKE_PE_BYTES)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    dump_syms_calls: list[tuple[str, ...]] = []
    mock_exec = make_mock_exec(
        returncode=0,
        stdout=_sample_sym("ntdll.pdb", NTDLL_DEBUG_ID, NTDLL_CODE_ID),
        calls_log=dump_syms_calls,
    )
    candidate = WindowsSymbolCandidate(
        module_name="ntdll.dll",
        debug_file="ntdll.pdb",
        debug_id=NTDLL_DEBUG_ID,
        code_file="ntdll.dll",
        code_id=NTDLL_CODE_ID,
        filename=r"C:\Windows\System32\ntdll.dll",
    )

    summary = await fetch_and_import_windows_symbols(
        [candidate],
        storage_manager,
        test_settings,
        exec_fn=mock_exec,
        http_transport=transport,
    )
    assert summary.imported == 1
    assert summary.failed == 0
    assert summary.circuit_open is False
    assert all(ua == SYMSRV_USER_AGENT for ua in seen_user_agents)
    assert has_cached_symbol(storage_manager, "ntdll.pdb", NTDLL_DEBUG_ID)
    assert list(storage_manager.partition_path("staging").iterdir()) == []
    # Combined [pe, pdb] invocation was attempted
    assert any(len(cmd) == 3 for cmd in dump_syms_calls)

    # Second run hits local cache without any outbound HTTP requests.
    requested_urls.clear()
    summary2 = await fetch_and_import_windows_symbols(
        [candidate],
        storage_manager,
        test_settings,
        exec_fn=mock_exec,
        http_transport=transport,
    )
    assert summary2.cached_hit == 1
    assert summary2.imported == 0
    assert requested_urls == []


async def test_fetch_pe_404_falls_back_to_pdb_only(
    storage_manager: StorageManager,
    test_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/ntdll.pdb/{NTDLL_DEBUG_ID}/ntdll.pdb"):
            return httpx.Response(200, content=FAKE_PDB_BYTES)
        return httpx.Response(404)

    calls: list[tuple[str, ...]] = []
    mock_exec = make_mock_exec(
        returncode=0,
        stdout=_sample_sym("ntdll.pdb", NTDLL_DEBUG_ID),
        calls_log=calls,
    )
    cand = WindowsSymbolCandidate(
        module_name="ntdll.dll",
        debug_file="ntdll.pdb",
        debug_id=NTDLL_DEBUG_ID,
        code_file="ntdll.dll",
        code_id=NTDLL_CODE_ID,
        filename=r"C:\Windows\System32\ntdll.dll",
    )

    summary = await fetch_and_import_windows_symbols(
        [cand],
        storage_manager,
        test_settings,
        exec_fn=mock_exec,
        http_transport=httpx.MockTransport(handler),
    )
    assert summary.imported == 1
    assert len(calls) == 1 and len(calls[0]) == 2
    assert has_cached_symbol(storage_manager, "ntdll.pdb", NTDLL_DEBUG_ID)


async def test_fetch_cab_compressed_pd_fallback(
    storage_manager: StorageManager,
    test_settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/ntdll.pdb/{NTDLL_DEBUG_ID}/ntdll.pd_"):
            return httpx.Response(200, content=FAKE_CAB_BYTES)
        return httpx.Response(404)

    async def fake_cabextract(*cmd: str, **_kwargs: Any) -> Any:
        # cabextract -q -d <out_dir> <cab_path>
        out_dir = Path(cmd[3])
        (out_dir / "ntdll.pdb").write_bytes(FAKE_PDB_BYTES)

        class _Proc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b""

            def kill(self) -> None:
                pass

        return _Proc()

    cand = WindowsSymbolCandidate(
        module_name="ntdll.dll",
        debug_file="ntdll.pdb",
        debug_id=NTDLL_DEBUG_ID,
        filename=r"C:\Windows\System32\ntdll.dll",
    )
    settings = test_settings.model_copy(update={"MS_SYMBOL_FETCH_PE_UNWIND": False})
    summary = await fetch_and_import_windows_symbols(
        [cand],
        storage_manager,
        settings,
        exec_fn=make_mock_exec(returncode=0, stdout=_sample_sym("ntdll.pdb", NTDLL_DEBUG_ID)),
        cab_exec_fn=fake_cabextract,
        http_transport=httpx.MockTransport(handler),
    )
    assert summary.imported == 1
    assert has_cached_symbol(storage_manager, "ntdll.pdb", NTDLL_DEBUG_ID)


async def test_fetch_rejects_oversized_bad_magic_and_debug_id_mismatch(
    storage_manager: StorageManager,
    test_settings: Settings,
) -> None:
    # 1. Oversized Content-Length (> 1 MiB)
    small_cap = test_settings.model_copy(
        update={"MS_SYMBOL_MAX_PDB_MB": 1, "MS_SYMBOL_FETCH_PE_UNWIND": False}
    )
    cand1 = WindowsSymbolCandidate(
        module_name="ntdll.dll",
        debug_file="ntdll.pdb",
        debug_id=NTDLL_DEBUG_ID,
        filename=r"C:\Windows\System32\ntdll.dll",
    )
    res_big = await fetch_and_import_windows_symbols(
        [cand1],
        storage_manager,
        small_cap,
        exec_fn=make_mock_exec(returncode=0, stdout=_sample_sym("ntdll.pdb", NTDLL_DEBUG_ID)),
        http_transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                200, headers={"Content-Length": str(5 * 1024 * 1024)}, content=b"x"
            )
        ),
    )
    assert res_big.imported == 0 and res_big.failed == 1
    assert not has_cached_symbol(storage_manager, "ntdll.pdb", NTDLL_DEBUG_ID)

    # 2. Invalid magic (HTML error page)
    cand2 = WindowsSymbolCandidate(
        module_name="kernelbase.dll",
        debug_file="kernelbase.pdb",
        debug_id=KERNELBASE_DEBUG_ID,
        filename=r"C:\Windows\System32\kernelbase.dll",
    )
    res_html = await fetch_and_import_windows_symbols(
        [cand2],
        storage_manager,
        small_cap,
        exec_fn=make_mock_exec(
            returncode=0, stdout=_sample_sym("kernelbase.pdb", KERNELBASE_DEBUG_ID)
        ),
        http_transport=httpx.MockTransport(
            lambda _req: httpx.Response(200, content=b"<html><body>Access Denied</body></html>" * 4)
        ),
    )
    assert res_html.imported == 0 and res_html.failed == 1
    assert is_negative_cached(storage_manager, "kernelbase.pdb", KERNELBASE_DEBUG_ID)

    # 3. dump_syms output debug_id mismatch
    cand3 = WindowsSymbolCandidate(
        module_name="d3d11.dll",
        debug_file="d3d11.pdb",
        debug_id=D3D11_DEBUG_ID,
        filename=r"C:\Windows\System32\d3d11.dll",
    )
    res_mismatch = await fetch_and_import_windows_symbols(
        [cand3],
        storage_manager,
        small_cap,
        exec_fn=make_mock_exec(
            returncode=0,
            stdout=_sample_sym("d3d11.pdb", "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF1"),
        ),
        http_transport=httpx.MockTransport(
            lambda _req: httpx.Response(200, content=FAKE_PDB_BYTES)
        ),
    )
    assert res_mismatch.imported == 0 and res_mismatch.failed == 1
    assert not has_cached_symbol(storage_manager, "d3d11.pdb", D3D11_DEBUG_ID)
    assert is_negative_cached(storage_manager, "d3d11.pdb", D3D11_DEBUG_ID)


async def test_negative_cache_circuit_breaker_and_disk_floor_guard(
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    calls = 0

    def not_found_handler(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    cand = WindowsSymbolCandidate(
        module_name="ntdll.dll",
        debug_file="ntdll.pdb",
        debug_id=NTDLL_DEBUG_ID,
        filename=r"C:\Windows\System32\ntdll.dll",
    )
    settings = test_settings.model_copy(update={"MS_SYMBOL_FETCH_PE_UNWIND": False})

    # 404 writes negative cache and must NOT trip circuit breaker.
    s1 = await fetch_and_import_windows_symbols(
        [cand],
        storage_manager,
        settings,
        http_transport=httpx.MockTransport(not_found_handler),
    )
    assert s1.not_found == 1 and not is_circuit_open(storage_manager)
    assert is_negative_cached(storage_manager, "ntdll.pdb", NTDLL_DEBUG_ID)
    assert calls >= 1

    # Second attempt skips immediately without HTTP requests.
    calls = 0
    s2 = await fetch_and_import_windows_symbols(
        [cand],
        storage_manager,
        settings,
        http_transport=httpx.MockTransport(not_found_handler),
    )
    assert s2.skipped == 1 and calls == 0

    # Consecutive connectivity failures trip the circuit breaker.
    net_calls = 0

    def connect_fail_handler(req: httpx.Request) -> httpx.Response:
        nonlocal net_calls
        net_calls += 1
        raise httpx.ConnectError("offline", request=req)

    fail_cands = [
        WindowsSymbolCandidate(
            module_name="kernelbase.dll",
            debug_file="kernelbase.pdb",
            debug_id=f"{i:032X}1",
            filename=r"C:\Windows\System32\kernelbase.dll",
        )
        for i in range(1, CIRCUIT_FAILURE_THRESHOLD + 2)
    ]
    for c in fail_cands[:CIRCUIT_FAILURE_THRESHOLD]:
        await fetch_and_import_windows_symbols(
            [c],
            storage_manager,
            settings,
            http_transport=httpx.MockTransport(connect_fail_handler),
        )
    assert is_circuit_open(storage_manager)

    net_calls = 0
    s_open = await fetch_and_import_windows_symbols(
        [fail_cands[-1]],
        storage_manager,
        settings,
        http_transport=httpx.MockTransport(connect_fail_handler),
    )
    assert s_open.circuit_open is True and net_calls == 0

    # Disk host safety floor breached -> skips gracefully without outbound HTTP.
    low_disk_storage = StorageManager(
        data_dir=tmp_path / "low_disk",
        settings=settings,
        disk_usage_fn=make_disk_usage_fn(free_gb=1.0),
    )
    low_calls = 0

    def should_not_be_called(_req: httpx.Request) -> httpx.Response:
        nonlocal low_calls
        low_calls += 1
        return httpx.Response(200, content=FAKE_PDB_BYTES)

    s_disk = await fetch_and_import_windows_symbols(
        [cand],
        low_disk_storage,
        settings,
        http_transport=httpx.MockTransport(should_not_be_called),
    )
    assert s_disk.skipped == 1 and low_calls == 0


async def test_parse_crash_dispatches_ms_fetch_and_auto_resymbolicates_without_loop(
    tmp_path: Path,
    test_settings: Settings,
    storage_manager: StorageManager,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'msdl_e2e.db'}"
    engine = create_async_engine_from_url(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    settings = test_settings.model_copy(
        update={
            "DATABASE_URL": database_url,
            "REDIS_URL": "memory://",
            "AI_TRIAGE_MODE": "MANUAL",
            "MS_SYMBOL_FETCH_PE_UNWIND": False,
        }
    )
    bus = InMemoryEventBus()
    stackwalk_runs: list[tuple[str, ...]] = []
    msdl_http_calls: list[str] = []

    def http_handler(request: httpx.Request) -> httpx.Response:
        msdl_http_calls.append(str(request.url))
        if request.url.path.endswith(f"/ntdll.pdb/{NTDLL_DEBUG_ID}/ntdll.pdb"):
            return httpx.Response(200, content=FAKE_PDB_BYTES)
        return httpx.Response(404)

    async def unified_exec(*cmd: str, **_kwargs: Any) -> Any:
        bin_name = cmd[0]
        if bin_name == settings.DUMP_SYMS_BIN:
            stdout = _sample_sym("ntdll.pdb", NTDLL_DEBUG_ID)
        else:
            stackwalk_runs.append(cmd)
            resolved = has_cached_symbol(storage_manager, "ntdll.pdb", NTDLL_DEBUG_ID)
            payload = {
                "crash_info": {"type": "EXCEPTION_ACCESS_VIOLATION_READ", "address": "0x0"},
                "system_info": {"os": "Windows NT", "os_ver": "10.0.22631", "cpu": "amd64"},
                "crashing_thread": {
                    "threads_index": 0,
                    "frames": [
                        {
                            "frame": 0,
                            "offset": "0x7ff900001000",
                            "module": "ntdll.dll",
                            "function": "KiUserExceptionDispatcher" if resolved else None,
                            "missing_symbols": not resolved,
                        }
                    ],
                },
                "threads": [
                    {
                        "thread_id": 1,
                        "frames": [
                            {
                                "frame": 0,
                                "offset": "0x7ff900001000",
                                "module": "ntdll.dll",
                                "function": "KiUserExceptionDispatcher" if resolved else None,
                                "missing_symbols": not resolved,
                            }
                        ],
                    }
                ],
                "modules": [
                    {
                        "filename": r"C:\Windows\System32\ntdll.dll",
                        "debug_file": "ntdll.pdb",
                        "debug_id": NTDLL_DEBUG_ID,
                        "code_file": "ntdll.dll",
                        "code_id": NTDLL_CODE_ID,
                        "loaded_symbols": resolved,
                        "missing_symbols": not resolved,
                    }
                ],
            }
            stdout = json.dumps(payload).encode("utf-8")

        class _Proc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return stdout, b""

            def kill(self) -> None:
                pass

        return _Proc()

    dispatcher = TaskDispatcher(
        settings=settings,
        storage=storage_manager,
        event_bus=bus,
        session_factory=factory,
        exec_fn=unified_exec,
        http_transport=httpx.MockTransport(http_handler),
    )

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            project = Project(key="msdl-proj", name="MSDL Test", api_key_hash="hash")
            session.add(project)
            await session.flush()
            crash_id = uuid.uuid4()
            raw_dmp = tmp_path / "win.dmp"
            raw_dmp.write_bytes(b"MDMP_FAKE")
            dump_path, _, _ = storage_manager.compress_dump(raw_dmp, crash_id)
            session.add(
                CrashReport(
                    id=crash_id,
                    project_id=project.id,
                    status="PENDING",
                    app_version="1.0.0",
                    dump_path=dump_path,
                )
            )
            await session.flush()
            job_id = stage_crash_job(session, crash_id, "parse_crash")
            await session.commit()

        await dispatcher.kick_crash_jobs([job_id])
        # Wait for parse_crash -> fetch_ms_symbols -> auto-resymbolicate chain to settle.
        for _ in range(50):
            if dispatcher.pending_count == 0:
                async with factory() as session:
                    remaining = (await session.scalars(select(CrashJob))).all()
                if not remaining:
                    break
            await asyncio.sleep(0.05)

        assert len(msdl_http_calls) == 1
        assert len(stackwalk_runs) == 2
        assert has_cached_symbol(storage_manager, "ntdll.pdb", NTDLL_DEBUG_ID)

        async with factory() as session:
            crash = await session.get(CrashReport, crash_id)
            assert crash is not None
            assert crash.status == "ANALYZED"
            assert crash.missing_symbols_json == []
            frame0 = crash.parsed_stacks_json["threads"][0]["frames"][0]
            assert frame0["function"] == "KiUserExceptionDispatcher"
    finally:
        await dispatcher.drain_and_cancel()
        await engine.dispose()


async def test_fetch_ms_symbols_preserves_existing_ai_run_and_skips_active_crash(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'msdl_ai.db'}"
    engine = create_async_engine_from_url(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    settings = test_settings.model_copy(
        update={"DATABASE_URL": database_url, "MS_SYMBOL_FETCH_PE_UNWIND": False}
    )

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            project = Project(key="ai-guard", name="AI Guard", api_key_hash="hash")
            session.add(project)
            await session.flush()
            crash_id = uuid.uuid4()
            raw_dmp = tmp_path / "ai.dmp"
            raw_dmp.write_bytes(b"MDMP_AI")
            dump_path, _, _ = storage_manager.compress_dump(raw_dmp, crash_id)
            session.add(
                CrashReport(
                    id=crash_id,
                    project_id=project.id,
                    status="ANALYZED",
                    app_version="1.0.0",
                    dump_path=dump_path,
                )
            )
            session.add(
                AIAnalysisRun(
                    crash_id=crash_id,
                    model_name="gpt-4o-mini",
                    status="COMPLETED",
                )
            )
            await session.commit()

        ctx = {
            "settings": settings,
            "session_factory": factory,
            "storage": storage_manager,
            "event_bus": InMemoryEventBus(),
            "exec_fn": make_mock_exec(
                returncode=0, stdout=_sample_sym("ntdll.pdb", NTDLL_DEBUG_ID)
            ),
            "http_transport": httpx.MockTransport(
                lambda _req: httpx.Response(200, content=FAKE_PDB_BYTES)
            ),
        }
        cand = WindowsSymbolCandidate(
            module_name="ntdll.dll",
            debug_file="ntdll.pdb",
            debug_id=NTDLL_DEBUG_ID,
            filename=r"C:\Windows\System32\ntdll.dll",
        )
        res = await fetch_ms_symbols(ctx, crash_id, project.id, [cand.to_dict()])
        assert res["imported"] == 1
        assert res["resymbolicate_queued"] is True

        async with factory() as session:
            job = (await session.scalars(select(CrashJob))).one()
            assert job.task == "resymbolicate"
            # Existing AIAnalysisRun prevents re-spending LLM tokens on auto-resymbolicate.
            assert job.run_ai_override is False
    finally:
        await engine.dispose()


def test_parse_manual_ms_target_and_pe_code_id_normalization() -> None:
    gc_did = "90D28435EAAB109449C47402C53C42D31"
    cand = parse_manual_ms_target("GraphicsCapture.dll", gc_did)
    assert cand.module_name == "GraphicsCapture.dll"
    assert cand.debug_file == "GraphicsCapture.pdb"
    assert cand.debug_id == gc_did
    assert cand.code_file == "GraphicsCapture.dll"

    dashed = parse_manual_ms_target(
        "GraphicsCapture.pdb",
        "{90d28435-eaab-1094-49c4-7402c53c42d3}",
        age="1",
        code_id="65A1B2C30001A000",
    )
    assert dashed.debug_file == "GraphicsCapture.pdb"
    assert dashed.debug_id == gc_did
    assert dashed.code_id == "65A1B2C30001A000"

    assert _normalize_pe_code_ids("65A1B2C30001A000") == [
        "65A1B2C31a000",
        "65A1B2C30001A000",
    ]
    assert _normalize_pe_code_ids("65A1B2C31a000") == [
        "65A1B2C31a000",
        "65A1B2C31A000",
    ]

    assert is_windows_system_module(
        filename=None,
        code_file=None,
        module_name="GraphicsCapture.dll",
        debug_file="GraphicsCapture.pdb",
    )


async def test_explicit_force_sync_bypasses_allowlist_normalizes_urls_and_upgrades_pe(
    storage_manager: StorageManager,
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    from app.db.models import Symbol

    gc_did = "90D28435EAAB109449C47402C53C42D31"
    raw_code_id = "65A1B2C30001A000"
    canonical_code_id = "65A1B2C31a000"
    requested_paths: list[str] = []
    serve_pe = {"enabled": False}

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path.endswith(
            f"/graphicscapture.pdb/{gc_did}/graphicscapture.pdb"
        ):
            return httpx.Response(200, content=FAKE_PDB_BYTES)
        if serve_pe["enabled"] and request.url.path.endswith(
            f"/graphicscapture.dll/{canonical_code_id}/graphicscapture.dll"
        ):
            return httpx.Response(200, content=FAKE_PE_BYTES)
        return httpx.Response(404)

    async def fake_dump_syms(*cmd: str, **_kwargs: Any) -> Any:
        has_pe = len(cmd) >= 3
        out = _sample_sym(
            "GraphicsCapture.pdb",
            gc_did,
            canonical_code_id if has_pe else None,
        )

        class _Proc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return out, b""

            def kill(self) -> None:
                pass

        return _Proc()

    cand = parse_manual_ms_target(
        "GraphicsCapture.dll",
        gc_did,
        code_id=raw_code_id,
    )
    # 1. Initial sync without PE imports PDB-only and stores global Symbol row
    s1 = await fetch_and_import_windows_symbols(
        [cand],
        storage_manager,
        test_settings.model_copy(update={"MS_SYMBOL_AUTO_FETCH": False}),
        session=db_session,
        explicit_targets=True,
        force=True,
        bypass_auto_fetch_flag=True,
        exec_fn=fake_dump_syms,
        http_transport=httpx.MockTransport(handler),
    )
    assert s1.imported == 1
    assert s1.items[0].status == "imported"
    assert s1.items[0].has_pe_unwind is False
    assert any(
        p.endswith(f"/graphicscapture.pdb/{gc_did}/graphicscapture.pdb")
        for p in requested_paths
    )
    assert any(
        p.endswith(f"/graphicscapture.dll/{canonical_code_id}/graphicscapture.dll")
        for p in requested_paths
    )

    found, scope, _ = has_cached_symbol_detail(
        storage_manager, "GraphicsCapture.pdb", gc_did
    )
    assert found is True
    assert scope == "public"

    row = (
        await db_session.scalars(
            select(Symbol).where(Symbol.debug_id == gc_did)
        )
    ).one()
    assert row.project_id is None
    assert row.app_version is None
    assert row.source == "msdl"
    assert row.has_pe_unwind is False

    # 2. Second force sync once PE is available upgrades existing public symbol
    serve_pe["enabled"] = True
    s2 = await fetch_and_import_windows_symbols(
        [cand],
        storage_manager,
        test_settings,
        session=db_session,
        explicit_targets=True,
        force=True,
        bypass_auto_fetch_flag=True,
        exec_fn=fake_dump_syms,
        http_transport=httpx.MockTransport(handler),
    )
    assert s2.upgraded == 1
    assert s2.items[0].status == "upgraded"
    assert s2.items[0].has_pe_unwind is True
    await db_session.refresh(row)
    assert row.has_pe_unwind is True

