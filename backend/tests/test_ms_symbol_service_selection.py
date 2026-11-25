"""Unit tests for MSDL URL parsing, allowlist filtering, privacy, and candidate selection."""

from __future__ import annotations

import uuid

from app.config import Settings
from app.services.ms_symbol_service import (
    _is_valid_pdb_identity,
    _normalize_pe_code_ids,
    is_windows_system_module,
    parse_manual_ms_target,
    select_windows_system_candidates,
    split_public_symbol_urls,
)
from app.services.storage import StorageManager
from tests._ms_symbol_helpers import (
    D3D11_DEBUG_ID,
    KERNELBASE_DEBUG_ID,
    NTDLL_CODE_ID,
    NTDLL_DEBUG_ID,
    _sample_sym,
)


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

    assert _is_valid_pdb_identity("ntdll.pdb", gc_did) is True
    assert _is_valid_pdb_identity(".pdb", gc_did) is False
    assert _is_valid_pdb_identity(" ntdll.pdb", gc_did) is False
    assert _is_valid_pdb_identity("ntdll:bad.pdb", gc_did) is False
    assert _is_valid_pdb_identity("ntdll.pdb", gc_did[:32]) is False
    assert _is_valid_pdb_identity("ntdll.pdb", "0" * 33) is False
    assert _is_valid_pdb_identity("ntdll.pdb", "0" * 32 + "1") is False
    assert _is_valid_pdb_identity("ntdll.pdb", "G" * 33) is False

    for bad_did in ("0" * 32, "0" * 33, "0" * 32 + "1", "G" * 33):
        try:
            parse_manual_ms_target("ntdll.pdb", bad_did)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected ValueError for bad debug_id={bad_did!r}")


def test_expanded_windows_system_modules_and_privacy_invariants() -> None:
    # Real Windows System32 DLLs seen as bare basenames from minidump-stackwalk
    for mod, pdb in (
        ("msvcp_win.dll", "msvcp_win.pdb"),
        ("kernel.appcore.dll", "Kernel.Appcore.pdb"),
        ("secur32.dll", "secur32.pdb"),
        ("wintrust.dll", "wintrust.pdb"),
        ("fwpuclnt.dll", "fwpuclnt.pdb"),
        ("clbcatq.dll", "CLBCatQ.pdb"),
        ("AudioSes.dll", "audioses.pdb"),
        ("bcryptPrimitives.dll", "bcryptprimitives.pdb"),
    ):
        assert is_windows_system_module(
            filename=mod,
            code_file=mod,
            module_name=mod,
            debug_file=pdb,
        ), f"Expected Windows system module: {mod}"

    # PDB-only fallback still detects known Windows modules
    assert is_windows_system_module(
        filename=None,
        code_file=None,
        module_name=None,
        debug_file="msvcp_win.pdb",
    )

    # Third-party and proprietary DLLs must remain excluded
    for mod, pdb in (
        ("nvwgf2umx.dll", "nvwgf2umx.pdb"),
        ("nvEncodeAPI64.dll", "nvEncodeAPI64.pdb"),
        ("boost_log-vc143-mt-x64-1_86.dll", "boost_log-vc143-mt-x64-1_86.pdb"),
        ("libcurl.dll", "libcurl.pdb"),
        ("LSRTMP.dll", "LSRTMP.pdb"),
        ("opus.dll", "opus.pdb"),
        ("zlib1.dll", "zlib.pdb"),
    ):
        assert not is_windows_system_module(
            filename=mod,
            code_file=mod,
            module_name=mod,
            debug_file=pdb,
        ), f"Unexpected match for third-party module: {mod}"
