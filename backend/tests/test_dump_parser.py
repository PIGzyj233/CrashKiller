"""Unit tests for MiniDump parsing and rust-minidump JSON normalization."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.services.dump_parser import DumpParseError, normalize_stackwalk_json, parse_dump
from app.services.storage import StorageManager
from tests.conftest import make_mock_exec

SAMPLE_STACKWALK_JSON = {
    "crash_info": {
        "type": "EXCEPTION_ACCESS_VIOLATION_READ",
        "address": "0x0000000000000018",
    },
    "system_info": {
        "os": "Windows NT",
        "os_ver": "10.0.22631",
        "cpu": "amd64",
    },
    "crashing_thread": {
        "threads_index": 0,
        "frames": [
            {
                "frame": 0,
                "offset": "0x7ff612341050",
                "module": "Game.exe",
                "module_offset": "0x1050",
                "function": "Renderer::SubmitDrawCall",
                "function_offset": "0x20",
                "file": "C:\\src\\renderer.cpp",
                "line": 142,
                "trust": "cfi",
                "registers": {"rax": "0x0", "rip": "0x7ff612341050"},
                "inlines": [
                    {
                        "function": "Texture::Bind",
                        "file": "C:\\src\\texture.h",
                        "line": 31,
                        "function_offset": "0x4",
                    }
                ],
            }
        ],
    },
    "threads": [
        {
            "thread_id": 1001,
            "thread_name": "RenderThread",
            "frames": [
                {
                    "frame": 0,
                    "offset": "0x7ff612341050",
                    "module": "Game.exe",
                    "module_offset": "0x1050",
                    "function": "Renderer::SubmitDrawCall",
                    "function_offset": "0x20",
                    "file": "C:\\src\\renderer.cpp",
                    "line": 142,
                    "trust": "cfi",
                },
                {
                    "frame": 1,
                    "offset": "0x7ff612342000",
                    "module": "Plugin.dll",
                    "missing_symbols": True,
                    "trust": "scan",
                },
            ],
        },
        {
            "thread_id": 1002,
            "thread_name": "MainThread",
            "frames": [],
        },
    ],
    "modules": [
        {
            "filename": "C:\\Program Files\\Game\\Game.exe",
            "base_addr": "0x7ff612340000",
            "end_addr": "0x7ff612400000",
            "version": "1.2.3.4",
            "debug_file": "Game.pdb",
            "debug_id": "11223344556677889900aabbccddeeff1",
            "code_file": "Game.exe",
            "code_id": "654321abcdef",
            "loaded_symbols": True,
            "corrupt_symbols": False,
            "missing_symbols": False,
        },
        {
            "filename": "C:\\Program Files\\Game\\Plugin.dll",
            "debug_file": "Plugin.pdb",
            "debug_id": "deadbeef1234",
            "code_file": "Plugin.dll",
            "code_id": "99887766",
            "loaded_symbols": False,
            "missing_symbols": True,
        },
    ],
}


async def test_parse_dump_zst_normalization_and_cleanup(
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    raw_dmp = tmp_path / "crash.dmp"
    raw_dmp.write_bytes(b"MDMP_TEST_PAYLOAD" * 64)
    rel_zst, _, _ = storage_manager.compress_dump(raw_dmp, "crash-123")

    calls: list[tuple[str, ...]] = []
    exec_ok = make_mock_exec(
        returncode=0,
        stdout=json.dumps(SAMPLE_STACKWALK_JSON).encode("utf-8"),
        calls_log=calls,
    )

    result = await parse_dump(rel_zst, storage_manager, test_settings, exec_fn=exec_ok)

    # Staging dir cleaned up
    assert list(storage_manager.partition_path("staging").iterdir()) == []

    # Verify CLI flags included --json, --symbols-url (Breakpad only, excluding MSDL),
    # --symbols-cache, and public_cache as secondary local symbol path after sym_dir.
    cmd = calls[0]
    assert "--json" in cmd
    assert "--symbols-url" in cmd
    assert "--symbols-cache" in cmd
    assert "https://symbols.mozilla.org/" in cmd
    assert "https://msdl.microsoft.com/download/symbols/" not in cmd
    pub_cache_str = str(storage_manager.partition_path("symbols/public_cache"))
    assert cmd[-1] == pub_cache_str

    # Verify normalization
    assert result.crash_reason == "EXCEPTION_ACCESS_VIOLATION_READ"
    assert result.crash_address == "0x0000000000000018"
    assert result.os_version == "Windows NT 10.0.22631"
    assert result.cpu_arch == "amd64"
    assert result.crashing_thread_index == 0

    top = result.parsed_stacks_json["threads"][0]["frames"][0]
    assert top["frame_index"] == 0
    assert top["ip"] == "0x7ff612341050"
    assert top["trust"] == "cfi"
    assert top["registers"] == {"rax": "0x0", "rip": "0x7ff612341050"}
    assert top["inlines"][0]["function"] == "Texture::Bind"

    assert len(result.missing_symbols_json) == 1
    assert result.parsed_stacks_json["modules"][0]["module_name"] == "Game.exe"
    assert result.parsed_stacks_json["modules"][1]["module_name"] == "Plugin.dll"
    assert result.missing_symbols_json[0]["module_name"] == "Plugin.dll"
    assert result.missing_symbols_json[0]["debug_file"] == "Plugin.pdb"
    assert result.missing_symbols_json[0]["debug_id"] == "DEADBEEF1234"
    assert result.parsed_summary_json["missing_symbol_count"] == 1
    assert result.parsed_summary_json["thread_count"] == 2

    # Verify MSDL-only PUBLIC_SYMBOL_URLS omits --symbols-url/--symbols-cache but keeps local cache
    msdl_only_settings = test_settings.model_copy(
        update={"PUBLIC_SYMBOL_URLS": ["https://msdl.microsoft.com/download/symbols/"]}
    )
    calls_msdl_only: list[tuple[str, ...]] = []
    await parse_dump(
        rel_zst,
        storage_manager,
        msdl_only_settings,
        exec_fn=make_mock_exec(
            returncode=0,
            stdout=json.dumps(SAMPLE_STACKWALK_JSON).encode("utf-8"),
            calls_log=calls_msdl_only,
        ),
    )
    assert "--symbols-url" not in calls_msdl_only[0]
    assert "--symbols-cache" not in calls_msdl_only[0]
    assert calls_msdl_only[0][-1] == pub_cache_str

    # Verify ENABLE_PUBLIC_SYMBOLS=False omits --symbols-url, --symbols-cache, and public_cache
    no_pub_settings = test_settings.model_copy(update={"ENABLE_PUBLIC_SYMBOLS": False})
    calls_no_pub: list[tuple[str, ...]] = []
    await parse_dump(
        rel_zst,
        storage_manager,
        no_pub_settings,
        exec_fn=make_mock_exec(
            returncode=0,
            stdout=json.dumps(SAMPLE_STACKWALK_JSON).encode("utf-8"),
            calls_log=calls_no_pub,
        ),
    )
    assert "--symbols-url" not in calls_no_pub[0]
    assert "--symbols-cache" not in calls_no_pub[0]
    assert pub_cache_str not in calls_no_pub[0]


async def test_normalize_stackwalk_null_fields_and_errors(
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    # Partial/null fields should not raise KeyError/TypeError
    minimal = normalize_stackwalk_json(
        {
            "crash_info": None,
            "system_info": {"os": None, "os_ver": None},
            "crashing_thread": {
                "frames": [{"offset": "0x100", "registers": None, "inlines": None}],
            },
            "threads": None,
            "modules": None,
        }
    )
    assert minimal.crash_reason == "UNKNOWN"
    assert minimal.os_version is None
    assert len(minimal.parsed_stacks_json["threads"]) == 1
    assert minimal.parsed_stacks_json["threads"][0]["frames"][0]["ip"] == "0x100"

    raw_dmp = tmp_path / "bad.dmp"
    raw_dmp.write_bytes(b"MDMP")
    rel_zst, _, _ = storage_manager.compress_dump(raw_dmp, "crash-bad")

    # Non-zero exit
    with pytest.raises(DumpParseError, match="exited 1"):
        await parse_dump(
            rel_zst,
            storage_manager,
            test_settings,
            exec_fn=make_mock_exec(returncode=1, stderr=b"corrupt minidump"),
        )
    assert list(storage_manager.partition_path("staging").iterdir()) == []

    # Invalid JSON
    with pytest.raises(DumpParseError, match="Invalid JSON"):
        await parse_dump(
            rel_zst,
            storage_manager,
            test_settings,
            exec_fn=make_mock_exec(returncode=0, stdout=b"{not-json"),
        )
    assert list(storage_manager.partition_path("staging").iterdir()) == []

    # Timeout
    short_settings = test_settings.model_copy(update={"TOOL_TIMEOUT_SECONDS": 0.01})
    with pytest.raises(DumpParseError, match="timed out"):
        await parse_dump(
            rel_zst,
            storage_manager,
            short_settings,
            exec_fn=make_mock_exec(returncode=0, stdout=b"{}", delay=0.5),
        )
    assert list(storage_manager.partition_path("staging").iterdir()) == []


def test_normalize_windows_basename_and_null_missing_symbols() -> None:
    res = normalize_stackwalk_json(
        {
            "crash_info": {"type": "EXCEPTION_BREAKPOINT", "address": "0x1"},
            "threads": [],
            "modules": [
                {
                    "filename": "C:\\Program Files\\My Game\\Plugin.dll",
                    "debug_file": "Plugin.pdb",
                    "debug_id": "abcd1234",
                    "loaded_symbols": False,
                    "missing_symbols": None,
                },
                {
                    "filename": "/usr/lib/libengine.so",
                    "debug_file": "libengine.so",
                    "debug_id": "ef567890",
                    "loaded_symbols": True,
                    "missing_symbols": None,
                },
            ],
        }
    )
    mods = res.parsed_stacks_json["modules"]
    assert mods[0]["module_name"] == "Plugin.dll"
    assert mods[0]["missing_symbols"] is True
    assert mods[1]["module_name"] == "libengine.so"
    assert mods[1]["missing_symbols"] is False
    assert len(res.missing_symbols_json) == 1
    assert res.missing_symbols_json[0]["module_name"] == "Plugin.dll"


async def test_parse_dump_rejects_nul_byte_paths(
    storage_manager: StorageManager,
    test_settings: Settings,
) -> None:
    with pytest.raises(DumpParseError, match="NUL byte"):
        await parse_dump("dumps/evil\x00.dmp.zst", storage_manager, test_settings)

    with pytest.raises(DumpParseError, match="NUL byte"):
        await parse_dump("dumps/evil\x00.dmp", storage_manager, test_settings)


def test_normalize_frame_explicit_null_frame_index_falls_back_to_idx() -> None:
    res = normalize_stackwalk_json(
        {
            "threads": [
                {
                    "frames": [
                        {"frame": None, "frame_index": None, "offset": "0x1"},
                        {"frame_index": None, "offset": "0x2"},
                    ]
                }
            ]
        }
    )
    frames = res.parsed_stacks_json["threads"][0]["frames"]
    assert [f["frame_index"] for f in frames] == [0, 1]
    assert [f["ip"] for f in frames] == ["0x1", "0x2"]


async def test_parse_dump_corrupt_zst_and_malformed_json_fields(
    storage_manager: StorageManager,
    test_settings: Settings,
) -> None:
    corrupt_rel = "dumps/corrupt-zst/corrupt-zst.dmp.zst"
    storage_manager.atomic_write_bytes(corrupt_rel, b"NOT_VALID_ZSTD_DATA")
    with pytest.raises(DumpParseError):
        await parse_dump(corrupt_rel, storage_manager, test_settings)

    norm = normalize_stackwalk_json(
        {
            "crash_info": {
                "type": "R" * 300,
                "address": "0x" + ("A" * 200),
                "crashing_thread": 1,
            },
            "system_info": {"os": "Win", "os_ver": "10." + ("1" * 300)},
            "threads": [
                {"thread_name": "T0", "frames": [{"frame": "abc", "registers": ["bad"]}]},
                {"thread_name": "T1", "frames": [{"frame": 0, "function": "CrashHere()"}]},
            ],
        }
    )
    assert norm.crashing_thread_index == 1
    assert len(norm.crash_reason) == 255
    assert norm.crash_address is not None and len(norm.crash_address) == 128
    assert norm.os_version is not None and len(norm.os_version) == 255
    assert norm.parsed_stacks_json["threads"][0]["frames"][0]["frame_index"] == 0
    assert norm.parsed_stacks_json["threads"][0]["frames"][0]["registers"] == {}


async def test_cancelling_stackwalk_stops_and_reaps_process(
    tmp_path: Path, storage_manager: StorageManager, test_settings: Settings,
) -> None:
    running = asyncio.Event()
    killed = asyncio.Event()
    reaped = asyncio.Event()
    raw_dump = tmp_path / "sample.dmp"
    raw_dump.write_bytes(b"mock stackwalk input")

    class Stackwalk:
        async def communicate(self) -> tuple[bytes, bytes]:
            running.set()
            await killed.wait()
            reaped.set()
            return b"", b""

        def kill(self) -> None:
            killed.set()

    async def start_stackwalk(*args: str, **kwargs: Any) -> Stackwalk:
        return Stackwalk()

    parse = asyncio.create_task(
        parse_dump(raw_dump, storage_manager, test_settings, exec_fn=start_stackwalk),
    )
    await asyncio.wait_for(running.wait(), timeout=2)
    parse.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(parse, timeout=2)
    assert killed.is_set() and reaped.is_set()
    assert not list(storage_manager.partition_path("staging").iterdir())
