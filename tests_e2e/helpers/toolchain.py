from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .shim_scripts import FAKE_DUMP_SYMS_SCRIPT, FAKE_MINIDUMP_STACKWALK_SCRIPT


class FakeToolchain:
    def __init__(self, bin_dir: Path, fixtures_dir: Path, log_path: Path) -> None:
        self.bin_dir = bin_dir
        self.fixtures_dir = fixtures_dir
        self.log_path = log_path
        self.dump_syms_bin = bin_dir / "fake_dump_syms"
        self.minidump_stackwalk_bin = bin_dir / "fake_minidump_stackwalk"

        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.fixtures_dir.mkdir(parents=True, exist_ok=True)

        self.dump_syms_bin.write_text(FAKE_DUMP_SYMS_SCRIPT, encoding="utf-8")
        self.dump_syms_bin.chmod(0o755)
        self.minidump_stackwalk_bin.write_text(FAKE_MINIDUMP_STACKWALK_SCRIPT, encoding="utf-8")
        self.minidump_stackwalk_bin.chmod(0o755)

    def make_pdb(
        self,
        filename: str = "MyApp.pdb",
        debug_id: str = "46A0ADB3F299A70B4C4C44205044422E1",
        code_file: str = "MyApp.exe",
        code_id: str = "64EC878F867C000",
        functions: list[dict[str, Any]] | None = None,
        corrupt: bool = False,
        extra_bytes: bytes = b"",
        write_to: Path | None = None,
    ) -> bytes:
        meta = {
            "debug_file": filename,
            "debug_id": debug_id,
            "code_file": code_file,
            "code_id": code_id,
            "functions": functions,
            "corrupt": corrupt,
        }
        payload = b"FAKE_PDB:" + json.dumps(meta).encode() + b"\n" + extra_bytes
        if write_to is not None:
            write_to.parent.mkdir(parents=True, exist_ok=True)
            write_to.write_bytes(payload)
        return payload

    def make_pe(
        self,
        filename: str = "MyApp.exe",
        code_id: str = "64EC878F867C000",
        debug_file: str = "MyApp.pdb",
        debug_id: str = "46A0ADB3F299A70B4C4C44205044422E1",
        extra_bytes: bytes = b"",
        write_to: Path | None = None,
    ) -> bytes:
        meta = {
            "code_file": filename,
            "code_id": code_id,
            "debug_file": debug_file,
            "debug_id": debug_id,
        }
        payload = b"FAKE_PE:" + json.dumps(meta).encode() + b"\n" + extra_bytes
        if write_to is not None:
            write_to.parent.mkdir(parents=True, exist_ok=True)
            write_to.write_bytes(payload)
        return payload

    def make_dump(
        self,
        crash_reason: str = "EXCEPTION_ACCESS_VIOLATION_READ",
        crash_address: str = "0x0000000000000000",
        modules: list[dict[str, Any]] | None = None,
        threads: list[dict[str, Any]] | None = None,
        crashing_thread_index: int = 0,
        system_info: dict[str, Any] | None = None,
        corrupt: bool = False,
        extra_bytes: bytes = b"",
        write_to: Path | None = None,
    ) -> bytes:
        if modules is None:
            modules = [
                {
                    "filename": "MyApp.exe",
                    "debug_file": "MyApp.pdb",
                    "debug_id": "46A0ADB3F299A70B4C4C44205044422E1",
                    "code_file": "MyApp.exe",
                    "code_id": "64EC878F867C000",
                    "always_loaded": False,
                }
            ]
        if threads is None:
            default_mod = modules[0]["filename"] if modules else "MyApp.exe"
            threads = [
                {
                    "thread_name": "MainThread",
                    "frames": [
                        {
                            "module": default_mod,
                            "function": "Renderer::SubmitDrawCall(Mesh*)",
                            "file": "src/renderer.cpp",
                            "line": 148,
                        },
                        {
                            "module": default_mod,
                            "function": "App::Tick(float)",
                            "file": "src/app.cpp",
                            "line": 89,
                        },
                    ],
                }
            ]
        meta = {
            "crash_reason": crash_reason,
            "crash_address": crash_address,
            "modules": modules,
            "threads": threads,
            "crashing_thread_index": crashing_thread_index,
            "system_info": system_info,
            "corrupt": corrupt,
        }
        payload = b"FAKE_DMP:" + json.dumps(meta).encode() + b"\n" + extra_bytes
        if write_to is not None:
            write_to.parent.mkdir(parents=True, exist_ok=True)
            write_to.write_bytes(payload)
        return payload

    def invocations(self, tool: str | None = None) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        entries = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if tool is None or item.get("tool") == tool:
                entries.append(item)
        return entries

    def clear_invocations(self) -> None:
        if self.log_path.exists():
            self.log_path.unlink()
