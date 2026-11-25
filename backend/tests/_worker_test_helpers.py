"""Shared helpers for worker task unit tests."""

from __future__ import annotations

import json


def _sample_stackwalk_json(
    function_name: str | None = "App::CrashFunc()",
    missing: bool = False,
) -> bytes:
    frames = [
        {
            "frame": 0,
            "module": "App.exe",
            "module_offset": "0x1234",
            "function": function_name,
            "function_offset": "0x10" if function_name else None,
            "file": "main.cpp" if function_name else None,
            "line": 42 if function_name else None,
            "trust": "cfi",
            "missing_symbols": missing,
        }
    ]
    modules = [
        {
            "filename": "App.exe",
            "debug_file": "App.pdb",
            "debug_id": "AAAABBBBCCCCDDDD11112222333344441",
            "code_file": "App.exe",
            "code_id": "12345678",
            "loaded_symbols": not missing,
            "missing_symbols": missing,
        }
    ]
    payload = {
        "status": "OK",
        "crash_info": {
            "type": "EXCEPTION_ACCESS_VIOLATION_READ",
            "address": "0x0",
            "crashing_thread": 0,
        },
        "system_info": {"os": "Windows NT", "os_ver": "10.0.22621", "cpu_arch": "amd64"},
        "crashing_thread": {"thread_index": 0, "frames": frames},
        "threads": [{"thread_index": 0, "thread_id": 1, "thread_name": "main", "frames": frames}],
        "modules": modules,
    }
    return json.dumps(payload).encode("utf-8")
