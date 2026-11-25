"""Unit tests for MSDL PDB/PE/CAB fetch, validation, negative cache, and circuit breaker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.services.ms_symbol_service import (
    CIRCUIT_FAILURE_THRESHOLD,
    SYMSRV_USER_AGENT,
    WindowsSymbolCandidate,
    fetch_and_import_windows_symbols,
    has_cached_symbol,
    is_circuit_open,
    is_negative_cached,
)
from app.services.storage import StorageManager
from tests._ms_symbol_helpers import (
    D3D11_DEBUG_ID,
    FAKE_CAB_BYTES,
    FAKE_PDB_BYTES,
    FAKE_PE_BYTES,
    KERNELBASE_DEBUG_ID,
    NTDLL_CODE_ID,
    NTDLL_DEBUG_ID,
    _sample_sym,
)
from tests.conftest import make_disk_usage_fn, make_mock_exec


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
