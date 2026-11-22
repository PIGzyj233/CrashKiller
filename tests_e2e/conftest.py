from __future__ import annotations

import asyncio
import collections
import datetime as dt
import http.server
import importlib
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_DIR = REPO_ROOT / "frontend"
CLI_SCRIPT = REPO_ROOT / "cli" / "crashkiller_cli.py"
SEED_SCRIPT = BACKEND_DIR / "scripts" / "seed_demo_data.py"

_MARKERS = (
    "tier1: Tier 1 core feature verification tests",
    "tier2: Tier 2 boundary, quota, and error-path tests",
    "tier3: Tier 3 pairwise feature interaction tests",
    "tier4: Tier 4 full workflow and workload stress tests",
    "tier5: Tier 5 adversarial and security hardening tests",
    "slow: Long-running workload or frontend build tests",
    "frontend: Frontend TypeScript check and Vite production build tests",
)


def pytest_configure(config: Any) -> None:
    for marker in _MARKERS:
        config.addinivalue_line("markers", marker)
    for path_str in (str(REPO_ROOT), str(BACKEND_DIR)):
        if path_str not in sys.path:
            sys.path.insert(0, path_str)
    # Install singleton disk_usage proxy before any backend module binds shutil.disk_usage
    shutil.disk_usage = _DISK_USAGE_PROXY  # type: ignore[assignment]


def _require_app_flag() -> bool:
    return os.environ.get("CRASHKILLER_E2E_REQUIRE_APP", "").strip() == "1"


def _load_backend_public() -> tuple[Any, Any]:
    """Lazily load only public app entrypoint (create_app) and config (get_settings)."""
    main_file = BACKEND_DIR / "app" / "main.py"
    if not main_file.exists():
        msg = "backend/app/main.py not yet implemented"
        if _require_app_flag():
            pytest.fail(msg)
        pytest.skip(msg)

    try:
        main_mod = importlib.import_module("app.main")
        config_mod = importlib.import_module("app.config")
    except ModuleNotFoundError as exc:
        if exc.name not in ("app", "app.main"):
            raise
        main_mod = importlib.import_module("backend.app.main")
        config_mod = importlib.import_module("backend.app.config")

    create_app = getattr(main_mod, "create_app", None)
    if create_app is None:
        app_obj = getattr(main_mod, "app", None)
        if app_obj is None:
            raise AttributeError("app.main must export create_app() or app")
        create_app = lambda: app_obj  # noqa: E731

    get_settings = getattr(config_mod, "get_settings")
    return create_app, get_settings


# ---------------------------------------------------------------------------
# Fake External Toolchain Shims (DUMP_SYMS_BIN & MINIDUMP_STACKWALK_BIN)
# ---------------------------------------------------------------------------

_FAKE_DUMP_SYMS_SCRIPT = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path


def _log_invocation(args):
    log_path = os.environ.get("E2E_SHIM_LOG_PATH")
    if not log_path:
        return
    entry = {"tool": "dump_syms", "args": args}
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _parse_tagged_file(path_str, prefix):
    data = Path(path_str).read_bytes()
    if not data.startswith(prefix):
        return None
    first_line = data.split(b"\n", 1)[0]
    payload = first_line[len(prefix):]
    return json.loads(payload.decode("utf-8"))


def main():
    args = sys.argv[1:]
    _log_invocation(args)

    out_path = None
    positional = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-o", "--output") and i + 1 < len(args):
            out_path = args[i + 1]
            i += 2
        elif arg.startswith("--output="):
            out_path = arg.split("=", 1)[1]
            i += 1
        elif arg.startswith("-o="):
            out_path = arg.split("=", 1)[1]
            i += 1
        elif arg.startswith("-"):
            i += 1
        else:
            positional.append(arg)
            i += 1

    if not positional:
        sys.stderr.write("dump_syms: missing input file\n")
        return 2

    pdb_meta = None
    pe_meta = None
    for fpath in positional:
        if not os.path.exists(fpath):
            sys.stderr.write(f"dump_syms: file not found: {fpath}\n")
            return 1
        p = _parse_tagged_file(fpath, b"FAKE_PDB:")
        if p is not None:
            pdb_meta = p
            continue
        e = _parse_tagged_file(fpath, b"FAKE_PE:")
        if e is not None:
            pe_meta = e
            continue
        sys.stderr.write(f"dump_syms: corrupt or unrecognized binary format: {fpath}\n")
        return 1

    if pdb_meta is None:
        sys.stderr.write("dump_syms: no valid PDB file provided\n")
        return 1

    if pdb_meta.get("corrupt"):
        sys.stderr.write("dump_syms: simulated corrupt PDB error\n")
        return 1

    debug_file = pdb_meta.get("debug_file", "MyApp.pdb")
    debug_id = pdb_meta.get("debug_id", "46A0ADB3F299A70B4C4C44205044422E1")
    code_file = (pe_meta or pdb_meta).get("code_file", "MyApp.exe")
    code_id = (pe_meta or pdb_meta).get("code_id", "64EC878F867C000")
    os_name = pdb_meta.get("os", "windows")
    arch = pdb_meta.get("arch", "x86_64")

    lines = [f"MODULE {os_name} {arch} {debug_id} {debug_file}"]
    if code_id and code_file:
        lines.append(f"INFO CODE_ID {code_id} {code_file}")
    lines.append("FILE 0 src/app_main.cpp")

    funcs = pdb_meta.get("functions") or [
        {"addr": "1000", "size": "120", "name": "App::Run()", "line": 42},
        {"addr": "1200", "size": "80", "name": "Renderer::SubmitDrawCall(Mesh*)", "line": 148},
    ]
    for fn in funcs:
        addr = fn.get("addr", "1000")
        size = fn.get("size", "80")
        name = fn.get("name", "AppFunc()")
        line_no = fn.get("line", 10)
        lines.append(f"FUNC {addr} {size} 0 {name}")
        lines.append(f"{addr} 10 {line_no} 0")
        if pe_meta is not None:
            lines.append(f"STACK CFI INIT {addr} {size} .cfa: $rsp 8 + .ra: .cfa -8 + ^")
            lines.append(f"STACK CFI {addr} .cfa: $rsp 40 +")

    sym_text = "\n".join(lines) + "\n"
    if out_path:
        Path(out_path).write_text(sym_text, encoding="utf-8")
    else:
        sys.stdout.write(sym_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


_FAKE_MINIDUMP_STACKWALK_SCRIPT = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path


def _log_invocation(entry):
    log_path = os.environ.get("E2E_SHIM_LOG_PATH")
    if not log_path:
        return
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _find_sym_file(sym_dir, debug_file, debug_id):
    if not sym_dir or not debug_file or not debug_id:
        return None
    sym_root = Path(sym_dir)
    stem = debug_file[:-4] if debug_file.lower().endswith(".pdb") else debug_file
    candidate_bases = []
    for did in (debug_id, debug_id.upper()):
        b = sym_root / debug_file / did
        if b not in candidate_bases:
            candidate_bases.append(b)
    mod_dir = sym_root / debug_file
    if not mod_dir.is_dir() and sym_root.is_dir():
        df_low = debug_file.lower()
        for child in sym_root.iterdir():
            if child.is_dir() and child.name.lower() == df_low:
                mod_dir = child
                break
    if mod_dir.is_dir():
        did_low = debug_id.lower()
        for child in mod_dir.iterdir():
            if child.is_dir() and child.name.lower() == did_low and child not in candidate_bases:
                candidate_bases.append(child)
    for base in candidate_bases:
        for cand in (base / f"{stem}.sym", base / f"{debug_file}.sym"):
            if cand.is_file():
                return cand
        if base.is_dir():
            for cand in base.glob("*.sym"):
                return cand
    return None


def main():
    args = sys.argv[1:]
    symbols_urls = []
    symbols_cache = None
    positional = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--symbols-url" and i + 1 < len(args):
            symbols_urls.append(args[i + 1])
            i += 2
        elif arg.startswith("--symbols-url="):
            symbols_urls.append(arg.split("=", 1)[1])
            i += 1
        elif arg == "--symbols-cache" and i + 1 < len(args):
            symbols_cache = args[i + 1]
            i += 2
        elif arg.startswith("--symbols-cache="):
            symbols_cache = arg.split("=", 1)[1]
            i += 1
        elif arg.startswith("-"):
            i += 1
        else:
            positional.append(arg)
            i += 1

    dmp_path = positional[0] if len(positional) >= 1 else None
    sym_dir = positional[1] if len(positional) >= 2 else None

    _log_invocation({
        "tool": "minidump-stackwalk",
        "args": args,
        "symbols_urls": symbols_urls,
        "symbols_cache": symbols_cache,
        "dmp_path": dmp_path,
        "sym_dir": sym_dir,
    })

    if not dmp_path or not os.path.exists(dmp_path):
        sys.stderr.write("minidump-stackwalk: dump file not found\n")
        return 2

    raw = Path(dmp_path).read_bytes()
    if not raw.startswith(b"FAKE_DMP:"):
        sys.stderr.write("minidump-stackwalk: corrupt or invalid minidump header\n")
        return 1

    first_line = raw.split(b"\n", 1)[0]
    try:
        meta = json.loads(first_line[len(b"FAKE_DMP:"):].decode("utf-8"))
    except Exception as exc:
        sys.stderr.write(f"minidump-stackwalk: corrupt dump payload: {exc}\n")
        return 1

    if meta.get("corrupt"):
        sys.stderr.write("minidump-stackwalk: simulated corrupt minidump\n")
        return 1

    modules_in = meta.get("modules") or []
    resolved_by_module = {}
    has_cfi_by_module = {}
    modules_out = []

    for mod in modules_in:
        filename = mod.get("filename", "MyApp.exe")
        debug_file = mod.get("debug_file", "MyApp.pdb")
        debug_id = mod.get("debug_id", "46A0ADB3F299A70B4C4C44205044422E1")
        code_file = mod.get("code_file", filename)
        code_id = mod.get("code_id", "64EC878F867C000")
        always_loaded = bool(mod.get("always_loaded", False))

        sym_file = _find_sym_file(sym_dir, debug_file, debug_id)
        has_cfi = False
        loaded = False
        if sym_file is not None:
            loaded = True
            sym_content = sym_file.read_text(encoding="utf-8", errors="replace")
            has_cfi = "STACK CFI" in sym_content
        elif always_loaded:
            loaded = True
        elif symbols_urls and symbols_cache and mod.get("public_available"):
            loaded = True
            cache_dir = Path(symbols_cache) / debug_file / debug_id
            cache_dir.mkdir(parents=True, exist_ok=True)
            stem = debug_file[:-4] if debug_file.lower().endswith(".pdb") else debug_file
            (cache_dir / f"{stem}.sym").write_text(
                f"MODULE windows x86_64 {debug_id} {debug_file}\n", encoding="utf-8"
            )

        resolved_by_module[filename] = loaded
        has_cfi_by_module[filename] = has_cfi
        modules_out.append({
            "filename": filename,
            "base_addr": mod.get("base_addr", "0x00007ff712330000"),
            "end_addr": mod.get("end_addr", "0x00007ff712400000"),
            "version": mod.get("version", "1.0.0.0"),
            "code_file": code_file,
            "code_identifier": code_id,
            "code_id": code_id,
            "debug_file": debug_file,
            "debug_identifier": debug_id,
            "debug_id": debug_id,
            "loaded_symbols": loaded,
            "corrupt_symbols": False,
            "missing_symbols": not loaded,
        })

    def _build_frames(raw_frames):
        out = []
        for idx, fr in enumerate(raw_frames):
            mod_name = fr.get("module", "MyApp.exe")
            is_loaded = resolved_by_module.get(mod_name, True)
            has_cfi = has_cfi_by_module.get(mod_name, False)
            if is_loaded:
                trust = fr.get("trust") or ("cfi" if has_cfi else ("context" if idx == 0 else "fp"))
                frame_obj = {
                    "frame": idx,
                    "offset": fr.get("offset", f"0x00007ff71234{idx:04x}"),
                    "module": mod_name,
                    "module_offset": fr.get("module_offset", f"0x0001{idx:04x}"),
                    "function": fr.get("function"),
                    "function_offset": fr.get("function_offset", "0x10"),
                    "file": fr.get("file", "src/app.cpp"),
                    "line": fr.get("line", 100 + idx),
                    "trust": trust,
                    "missing_symbols": False,
                    "inlines": fr.get("inlines", []),
                }
            else:
                frame_obj = {
                    "frame": idx,
                    "offset": fr.get("offset", f"0x00007ff71234{idx:04x}"),
                    "module": mod_name,
                    "module_offset": fr.get("module_offset", f"0x0001{idx:04x}"),
                    "function": None,
                    "function_offset": None,
                    "file": None,
                    "line": None,
                    "trust": "context" if idx == 0 else "scan",
                    "missing_symbols": True,
                    "inlines": [],
                }
            if "registers" in fr:
                frame_obj["registers"] = fr["registers"]
            elif idx == 0:
                frame_obj["registers"] = {"rax": "0x0", "rip": frame_obj["offset"], "rsp": "0x000000100000"}
            out.append(frame_obj)
        return out

    threads_in = meta.get("threads") or []
    threads_out = []
    for t_idx, th in enumerate(threads_in):
        threads_out.append({
            "thread_name": th.get("thread_name", f"Thread-{t_idx}"),
            "frames": _build_frames(th.get("frames") or []),
        })

    crashing_idx = int(meta.get("crashing_thread_index", 0) or 0)
    valid_crashing = 0 <= crashing_idx < len(threads_out)
    crashing_frames = threads_out[crashing_idx]["frames"] if valid_crashing else []
    crashing_thread_name = threads_out[crashing_idx]["thread_name"] if valid_crashing else "MainThread"

    result = {
        "status": "OK",
        "crash_info": {
            "type": meta.get("crash_reason", "EXCEPTION_ACCESS_VIOLATION_READ"),
            "address": meta.get("crash_address", "0x0000000000000000"),
            "crashing_thread": crashing_idx,
        },
        "system_info": meta.get("system_info") or {
            "os": "Windows NT",
            "os_ver": "10.0.22621",
            "cpu": "amd64",
            "cpu_arch": "amd64",
            "cpu_info": "family 6 model 140 stepping 1",
            "cpu_count": 8,
        },
        "crashing_thread": {
            "threads_index": crashing_idx,
            "thread_name": crashing_thread_name,
            "frames": crashing_frames,
        },
        "threads": threads_out,
        "modules": modules_out,
    }
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


class FakeToolchain:
    def __init__(self, bin_dir: Path, fixtures_dir: Path, log_path: Path) -> None:
        self.bin_dir = bin_dir
        self.fixtures_dir = fixtures_dir
        self.log_path = log_path
        self.dump_syms_bin = bin_dir / "fake_dump_syms"
        self.minidump_stackwalk_bin = bin_dir / "fake_minidump_stackwalk"

        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.fixtures_dir.mkdir(parents=True, exist_ok=True)

        self.dump_syms_bin.write_text(_FAKE_DUMP_SYMS_SCRIPT, encoding="utf-8")
        self.dump_syms_bin.chmod(0o755)
        self.minidump_stackwalk_bin.write_text(_FAKE_MINIDUMP_STACKWALK_SCRIPT, encoding="utf-8")
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
        payload = b"FAKE_PDB:" + json.dumps(meta).encode("utf-8") + b"\n" + extra_bytes
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
        payload = b"FAKE_PE:" + json.dumps(meta).encode("utf-8") + b"\n" + extra_bytes
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
        payload = b"FAKE_DMP:" + json.dumps(meta).encode("utf-8") + b"\n" + extra_bytes
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


@pytest.fixture
def fake_toolchain(tmp_path: Path) -> FakeToolchain:
    return FakeToolchain(
        bin_dir=tmp_path / "bin",
        fixtures_dir=tmp_path / "fixtures",
        log_path=tmp_path / "shim_invocations.jsonl",
    )


# ---------------------------------------------------------------------------
# Mock OpenAI-Compatible HTTP Server (LLM_BASE_URL)
# ---------------------------------------------------------------------------


class MockLLMServer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.requests: list[dict[str, Any]] = []
        self._handler: Any = None
        self._delay_s: float = 0.0
        self._error_status: int | None = None
        self._error_message: str = "Mock LLM error"
        self._loop_forever_tool: tuple[str, dict[str, Any]] | None = None
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    def start(self) -> None:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.rstrip("/").endswith("/models"):
                    self._send_json(
                        200,
                        {
                            "object": "list",
                            "data": [{"id": "mock-gpt-4o-mini", "object": "model"}],
                        },
                    )
                    return
                self._send_json(404, {"error": {"message": "Not found"}})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    req_json = json.loads(raw_body.decode("utf-8"))
                except Exception:
                    req_json = {"raw": raw_body.decode("utf-8", errors="replace")}

                req_record = {
                    "method": "POST",
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": req_json,
                }
                with outer._lock:
                    outer.requests.append(req_record)
                    req_idx = len(outer.requests) - 1
                    delay_s = outer._delay_s
                    err_status = outer._error_status
                    err_msg = outer._error_message
                    handler_fn = outer._handler
                    loop_tool = outer._loop_forever_tool

                if delay_s > 0:
                    time.sleep(delay_s)

                if err_status is not None:
                    self._send_json(err_status, {"error": {"message": err_msg, "code": err_status}})
                    return

                if handler_fn is not None:
                    res = handler_fn(req_json, req_record["headers"], req_idx)
                    if isinstance(res, tuple):
                        status_code, body = res
                    else:
                        status_code, body = 200, res
                    self._send_json(status_code, body)
                    return

                try:
                    spec = outer._queue.get_nowait()
                except queue.Empty:
                    if loop_tool is not None:
                        t_name, t_args = loop_tool
                        spec = outer._make_tool_call_spec([(t_name, t_args)])
                    else:
                        spec = outer._make_verdict_spec()

                spec_delay = float(spec.get("delay_s", 0.0))
                if spec_delay > 0:
                    time.sleep(spec_delay)

                status = int(spec.get("status", 200))
                body = spec.get("body")
                if body is None:
                    body = outer._build_completion_payload(
                        model=req_json.get("model", "mock-gpt-4o-mini"),
                        content=spec.get("content"),
                        tool_calls=spec.get("tool_calls"),
                        finish_reason=spec.get("finish_reason", "stop"),
                    )
                self._send_json(status, body)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def _build_completion_payload(
        self,
        model: str,
        content: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        finish_reason: str = "stop",
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": 110,
                "completion_tokens": 42,
                "total_tokens": 152,
            },
        }

    def _make_tool_call_spec(self, calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
        tc_list = []
        for idx, (name, args) in enumerate(calls):
            tc_list.append(
                {
                    "id": f"call_{uuid.uuid4().hex[:10]}_{idx}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            )
        return {"status": 200, "content": None, "tool_calls": tc_list, "finish_reason": "tool_calls"}

    def _make_verdict_spec(
        self,
        verdict: str = "NEW_ISSUE",
        matched_issue_id: int | None = None,
        confidence: float = 0.91,
        suggested_title: str = "Renderer::SubmitDrawCall null pointer dereference",
        root_cause_analysis: str = "Null texture pointer passed to SubmitDrawCall after async load failure.",
        reasoning_summary: str = "Stack trace and error log match null pointer dereference pattern.",
        evidence_log_snippets: list[dict[str, Any]] | None = None,
        key_log_patterns: list[str] | None = None,
        suggested_component: str = "Rendering",
    ) -> dict[str, Any]:
        payload = {
            "verdict": verdict,
            "matched_issue_id": matched_issue_id,
            "confidence": confidence,
            "suggested_title": suggested_title,
            "root_cause_analysis": root_cause_analysis,
            "reasoning_summary": reasoning_summary,
            "evidence_log_snippets": evidence_log_snippets if evidence_log_snippets is not None else [],
            "key_log_patterns": key_log_patterns if key_log_patterns is not None else ["FATAL"],
            "suggested_component": suggested_component,
        }
        return {"status": 200, "content": json.dumps(payload), "finish_reason": "stop"}

    def queue_tool_call(self, tool_name: str, arguments: dict[str, Any] | None = None) -> None:
        self._queue.put(self._make_tool_call_spec([(tool_name, arguments or {})]))

    def queue_tool_calls(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self._queue.put(self._make_tool_call_spec(calls))

    def queue_verdict(
        self,
        verdict: str = "NEW_ISSUE",
        matched_issue_id: int | None = None,
        confidence: float = 0.91,
        suggested_title: str = "Renderer::SubmitDrawCall null pointer dereference",
        root_cause_analysis: str = "Null texture pointer passed to SubmitDrawCall.",
        reasoning_summary: str = "Crash stack and logs indicate null pointer dereference.",
        evidence_log_snippets: list[dict[str, Any]] | None = None,
        key_log_patterns: list[str] | None = None,
        suggested_component: str = "Rendering",
    ) -> None:
        self._queue.put(
            self._make_verdict_spec(
                verdict=verdict,
                matched_issue_id=matched_issue_id,
                confidence=confidence,
                suggested_title=suggested_title,
                root_cause_analysis=root_cause_analysis,
                reasoning_summary=reasoning_summary,
                evidence_log_snippets=evidence_log_snippets,
                key_log_patterns=key_log_patterns,
                suggested_component=suggested_component,
            )
        )

    def queue_response(
        self,
        status: int = 200,
        body: dict[str, Any] | None = None,
        content: str | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self._queue.put({"status": status, "body": body, "content": content, "delay_s": delay_s})

    def set_handler(self, fn: Any) -> None:
        with self._lock:
            self._handler = fn

    def set_delay(self, seconds: float) -> None:
        with self._lock:
            self._delay_s = seconds

    def set_error_status(self, status: int | None, message: str = "Mock LLM error") -> None:
        with self._lock:
            self._error_status = status
            self._error_message = message

    def set_loop_forever(self, tool_name: str = "get_crash_overview", arguments: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._loop_forever_tool = (tool_name, arguments or {})

    def reset(self) -> None:
        with self._lock:
            self.requests.clear()
            self._handler = None
            self._delay_s = 0.0
            self._error_status = None
            self._error_message = "Mock LLM error"
            self._loop_forever_tool = None
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break


@pytest.fixture
def mock_llm_server() -> MockLLMServer:
    srv = MockLLMServer()
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


# ---------------------------------------------------------------------------
# Fake Disk Usage (HOST_SAFETY_FLOOR_GB boundary control)
# ---------------------------------------------------------------------------


class FakeDiskUsage:
    _Usage = collections.namedtuple("usage", ["total", "used", "free"])

    def __init__(self, free_gb: float = 200.0, total_gb: float = 500.0) -> None:
        self._total_bytes = int(total_gb * (1024**3))
        self._free_bytes = int(free_gb * (1024**3))

    def set_free_gb(self, free_gb: float, total_gb: float = 500.0) -> None:
        self._total_bytes = int(total_gb * (1024**3))
        self._free_bytes = int(free_gb * (1024**3))

    def get_free_gb(self) -> float:
        return self._free_bytes / (1024**3)

    def __call__(self, path: Any) -> Any:
        used = max(0, self._total_bytes - self._free_bytes)
        return self._Usage(self._total_bytes, used, self._free_bytes)


_DISK_USAGE_PROXY = FakeDiskUsage(free_gb=200.0, total_gb=500.0)


@pytest.fixture(scope="session", autouse=True)
def _install_disk_usage_proxy() -> Any:
    orig = shutil.disk_usage
    shutil.disk_usage = _DISK_USAGE_PROXY  # type: ignore[assignment]
    try:
        yield _DISK_USAGE_PROXY
    finally:
        shutil.disk_usage = orig


@pytest.fixture
def fake_disk_usage(monkeypatch: pytest.MonkeyPatch) -> Any:
    _DISK_USAGE_PROXY.set_free_gb(200.0, 500.0)
    monkeypatch.setattr(shutil, "disk_usage", _DISK_USAGE_PROXY)
    try:
        yield _DISK_USAGE_PROXY
    finally:
        _DISK_USAGE_PROXY.set_free_gb(200.0, 500.0)


# ---------------------------------------------------------------------------
# Isolated Environment, ASGI App, AsyncClient, and Live Uvicorn Server
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_toolchain: FakeToolchain,
    mock_llm_server: MockLLMServer,
    fake_disk_usage: FakeDiskUsage,
) -> dict[str, Any]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "crashkiller_e2e.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"

    env_vars = {
        "DATABASE_URL": db_url,
        "REDIS_URL": "memory://",
        "DATA_DIR": str(data_dir),
        "CRASHKILLER_DATA_PATH": str(data_dir),
        "DUMP_SYMS_BIN": str(fake_toolchain.dump_syms_bin),
        "MINIDUMP_STACKWALK_BIN": str(fake_toolchain.minidump_stackwalk_bin),
        "ENABLE_PUBLIC_SYMBOLS": "false",
        "ARCHIVE_RAW_SYMBOLS": "false",
        "LLM_BASE_URL": mock_llm_server.base_url,
        "LLM_API_KEY": "e2e-test-key",
        "LLM_MODEL": "mock-gpt-4o-mini",
        "AI_TRIAGE_MODE": "SMART_TRIAGE",
        "AI_MAX_STEPS": "10",
        "AI_TIMEOUT_SECONDS": "30",
        "AI_TOOL_OUTPUT_MAX_CHARS": "8000",
        "HOST_SAFETY_FLOOR_GB": "50.0",
        "TOTAL_DISK_QUOTA_GB": "150.0",
        "E2E_SHIM_LOG_PATH": str(fake_toolchain.log_path),
    }
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)

    proc_env = os.environ.copy()
    proc_env.update(env_vars)
    proc_env["PYTHONPATH"] = f"{BACKEND_DIR}{os.pathsep}{REPO_ROOT}"

    return {
        "tmp_path": tmp_path,
        "data_dir": data_dir,
        "db_path": db_path,
        "db_url": db_url,
        "env_vars": env_vars,
        "proc_env": proc_env,
    }


@pytest.fixture
async def app_instance(isolated_env: dict[str, Any]) -> Any:
    create_app, get_settings = _load_backend_public()
    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()

    app = create_app()
    async with app.router.lifespan_context(app):
        yield app

    if hasattr(app, "dependency_overrides"):
        app.dependency_overrides.clear()
    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()


@pytest.fixture
async def client(app_instance: Any) -> Any:
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_instance),
        base_url="http://testserver",
        timeout=30.0,
    ) as ac:
        yield ac


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def live_server(isolated_env: dict[str, Any]) -> str:
    create_app, get_settings = _load_backend_public()
    import httpx
    import uvicorn

    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()

    port = _find_free_port()
    app = create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    ready = threading.Event()
    err_holder: list[BaseException] = []

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            ready.set()
            loop.run_until_complete(server.serve())
        except BaseException as exc:  # noqa: BLE001
            err_holder.append(exc)
        finally:
            loop.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    ready.wait(timeout=5.0)

    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15.0
        healthy = False
        while time.monotonic() < deadline:
            if err_holder:
                raise err_holder[0]
            try:
                resp = httpx.get(f"{base_url}/health", timeout=1.0)
                if resp.status_code == 200:
                    healthy = True
                    break
            except Exception:
                pass
            time.sleep(0.05)

        if not healthy:
            raise RuntimeError(f"live_server failed to become healthy on {base_url}")

        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        if hasattr(get_settings, "cache_clear"):
            get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Opaque-Box Test Helpers
# ---------------------------------------------------------------------------


class E2EHelpers:
    async def create_project(
        self,
        client: Any,
        key: str | None = None,
        name: str = "Test Project",
        settings_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        proj_key = key or f"proj-{uuid.uuid4().hex[:8]}"
        body: dict[str, Any] = {"key": proj_key, "name": name}
        if settings_json is not None:
            body["settings_json"] = settings_json
        resp = await client.post("/api/v1/projects", json=body)
        assert resp.status_code == 201, f"create_project failed: {resp.status_code} {resp.text}"
        return resp.json()

    async def upload_symbol(
        self,
        client: Any,
        project_key: str,
        api_key: str | None,
        pdb_bytes: bytes,
        pdb_filename: str = "MyApp.pdb",
        pe_bytes: bytes | None = None,
        pe_filename: str = "MyApp.exe",
        app_version: str = "1.0.0",
        use_header: bool = True,
        project_id: str | None = None,
    ) -> Any:
        data: dict[str, str] = {"app_version": app_version}
        if project_id is not None:
            data["project_id"] = project_id
        else:
            data["project_key"] = project_key
        headers: dict[str, str] = {}
        if api_key is not None:
            if use_header:
                headers["X-API-Key"] = api_key
            else:
                data["api_key"] = api_key

        files: list[tuple[str, tuple[str, bytes, str]]] = [
            ("pdb_file", (pdb_filename, pdb_bytes, "application/octet-stream"))
        ]
        if pe_bytes is not None:
            files.append(("pe_file", (pe_filename, pe_bytes, "application/octet-stream")))

        return await client.post("/api/v1/symbols/upload", data=data, files=files, headers=headers)

    async def upload_crash(
        self,
        client: Any,
        project_key: str,
        api_key: str | None,
        dump_bytes: bytes | None,
        dump_filename: str = "crash.dmp",
        log_files: list[tuple[str, bytes]] | None = None,
        app_version: str = "1.0.0",
        build_id: str | None = None,
        os_version: str | None = None,
        crash_time: str | None = None,
        use_header: bool = True,
        project_id: str | None = None,
    ) -> Any:
        data: dict[str, str] = {}
        if app_version is not None:
            data["app_version"] = app_version
        if project_id is not None:
            data["project_id"] = project_id
        elif project_key is not None:
            data["project_key"] = project_key
        if build_id is not None:
            data["build_id"] = build_id
        if os_version is not None:
            data["os_version"] = os_version
        if crash_time is not None:
            data["crash_time"] = crash_time

        headers: dict[str, str] = {}
        if api_key is not None:
            if use_header:
                headers["X-API-Key"] = api_key
            else:
                data["api_key"] = api_key

        files: list[tuple[str, tuple[str, bytes, str]]] = []
        if dump_bytes is not None:
            files.append(("dump_file", (dump_filename, dump_bytes, "application/octet-stream")))
        for fname, content in log_files or []:
            files.append(("log_files", (fname, content, "text/plain")))

        return await client.post("/api/v1/crashes/upload", data=data, files=files, headers=headers)

    async def wait_for_crash_status(
        self,
        client: Any,
        crash_id: str,
        target_statuses: tuple[str, ...] = ("ANALYZED", "FAILED"),
        timeout: float = 15.0,
        poll_interval: float = 0.05,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_body: Any = None
        while time.monotonic() < deadline:
            resp = await client.get(f"/api/v1/crashes/{crash_id}")
            if resp.status_code == 200:
                last_body = resp.json()
                if last_body.get("status") in target_statuses:
                    return last_body
            await asyncio.sleep(poll_interval)
        raise TimeoutError(
            f"Crash {crash_id} did not reach {target_statuses} within {timeout}s; last={last_body}"
        )

    async def stream_crash_sse(
        self,
        client: Any,
        crash_id: str,
        timeout: float = 15.0,
    ) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []

        async def _consume() -> None:
            async with client.stream(
                "GET",
                f"/api/v1/crashes/{crash_id}/events",
                headers={"Accept": "text/event-stream"},
            ) as resp:
                assert resp.status_code == 200, f"SSE status {resp.status_code}"
                ctype = resp.headers.get("content-type", "")
                assert "text/event-stream" in ctype, f"Unexpected SSE content-type: {ctype}"
                current_event = "message"
                data_lines: list[str] = []
                async for line in resp.aiter_lines():
                    if line == "":
                        if data_lines:
                            raw_data = "\n".join(data_lines)
                            parsed = json.loads(raw_data)
                            events.append((current_event, parsed))
                            if parsed.get("status") in ("ANALYZED", "FAILED"):
                                return
                        current_event = "message"
                        data_lines = []
                    elif line.startswith(":"):
                        continue
                    elif line.startswith("event:"):
                        current_event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line.split(":", 1)[1].strip())
                if data_lines:
                    raw_data = "\n".join(data_lines)
                    parsed = json.loads(raw_data)
                    events.append((current_event, parsed))

        await asyncio.wait_for(_consume(), timeout=timeout)
        return events

    def extract_log_id(self, tool_content: str, preferred_filename: str | None = None) -> str:
        try:
            payload = json.loads(tool_content)
            if isinstance(payload, dict):
                logs = payload.get("logs")
                if isinstance(logs, list):
                    if not logs:
                        return ""
                    if preferred_filename:
                        for entry in logs:
                            if isinstance(entry, dict) and entry.get("filename") == preferred_filename:
                                lid = entry.get("id") or entry.get("log_id")
                                if lid:
                                    return str(lid)
                    first = logs[0]
                    if isinstance(first, dict):
                        lid = first.get("id") or first.get("log_id")
                        if lid:
                            return str(lid)
                    return ""
        except Exception:
            pass
        m = re.search(
            r'"logs"\s*:\s*\[[^\]]*?\"(?:id|log_id)\"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"',
            tool_content,
            re.I | re.S,
        )
        return m.group(1) if m else ""

    def make_mixed_encoding_log(
        self,
        num_lines: int = 120,
        include_bom: bool = True,
        include_gbk: bool = True,
        include_invalid_bytes: bool = False,
        base_time: str = "2026-11-22T10:00:00",
        error_lines: dict[int, str] | None = None,
    ) -> bytes:
        base_dt = dt.datetime.fromisoformat(base_time)
        chunks: list[bytes] = []
        if include_bom:
            chunks.append(b"\xef\xbb\xbf")

        custom_errors = error_lines or {}
        for idx in range(1, num_lines + 1):
            ts = (base_dt + dt.timedelta(seconds=idx)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            if idx in custom_errors:
                line_bytes = f"{ts} {custom_errors[idx]}\n".encode("utf-8")
            elif include_gbk and idx % 7 == 0:
                line_bytes = f"{ts} ERROR 纹理加载失败 TextureCache fallback null 𠮷 第{idx}行\n".encode("gb18030")
            elif idx % 15 == 0:
                line_bytes = f"{ts} FATAL Renderer::SubmitDrawCall null texture pointer at line {idx}\n".encode("utf-8")
            elif idx % 5 == 0:
                line_bytes = f"{ts} WARN Frame budget exceeded line={idx}\n".encode("utf-8")
            else:
                line_bytes = f"{ts} INFO Worker heartbeat tick={idx} ok\n".encode("utf-8")

            if include_invalid_bytes and idx == num_lines // 2:
                line_bytes = f"{ts} WARN Corrupted bytes ".encode("utf-8") + b"\xff\xfe\x80" + b" recovered\n"
            chunks.append(line_bytes)

        return b"".join(chunks)

    def age_files(self, root_dir: Path, days_old: int = 40) -> int:
        if not root_dir.exists():
            return 0
        target_ts = time.time() - days_old * 86400
        count = 0
        for p in root_dir.rglob("*"):
            if p.is_file():
                os.utime(p, (target_ts, target_ts))
                count += 1
        return count

    def run_cli(
        self,
        args: list[str],
        env: dict[str, str],
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        if not CLI_SCRIPT.exists():
            msg = "cli/crashkiller_cli.py not yet implemented"
            if _require_app_flag():
                pytest.fail(msg)
            pytest.skip(msg)
        return subprocess.run(
            [sys.executable, str(CLI_SCRIPT), *args],
            env=env,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )


@pytest.fixture
def helpers() -> E2EHelpers:
    return E2EHelpers()
