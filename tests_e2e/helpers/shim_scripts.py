from __future__ import annotations

# Fake External Toolchain Shims (DUMP_SYMS_BIN & MINIDUMP_STACKWALK_BIN)

FAKE_DUMP_SYMS_SCRIPT = r"""#!/usr/bin/env python3
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

    input_meta = pdb_meta or pe_meta
    if input_meta.get("corrupt"):
        sys.stderr.write("dump_syms: simulated corrupt PDB error\n")
        return 1

    debug_file = input_meta.get("debug_file", "MyApp.pdb")
    debug_id = input_meta.get("debug_id", "46A0ADB3F299A70B4C4C44205044422E1")
    code_file = (pe_meta or input_meta).get("code_file", "MyApp.exe")
    code_id = (pe_meta or input_meta).get("code_id", "64EC878F867C000")
    os_name = input_meta.get("os", "windows")
    arch = input_meta.get("arch", "x86_64")

    lines = [f"MODULE {os_name} {arch} {debug_id} {debug_file}"]
    if code_id and code_file:
        lines.append(f"INFO CODE_ID {code_id} {code_file}")
    lines.append("FILE 0 src/app_main.cpp")

    funcs = input_meta.get("functions") or [
        {"addr": "1000", "size": "120", "name": "App::Run()", "line": 42},
        {
            "addr": "1200",
            "size": "80",
            "name": "Renderer::SubmitDrawCall(Mesh*)",
            "line": 148,
        },
    ]
    for fn in funcs:
        addr = fn.get("addr", "1000")
        size = fn.get("size", "80")
        name = fn.get("name", "AppFunc()")
        line_no = fn.get("line", 10)
        lines.append(f"FUNC {addr} {size} 0 {name}")
        lines.append(f"{addr} 10 {line_no} 0")
        if pe_meta is not None:
            lines.append(
                f"STACK CFI INIT {addr} {size} .cfa: $rsp 8 + .ra: .cfa -8 + ^"
            )
            lines.append(f"STACK CFI {addr} .cfa: $rsp 40 +")

    sym_text = "\n".join(lines) + "\n"
    if out_path:
        Path(out_path).write_text(sym_text, encoding="utf-8")
    else:
        sys.stdout.write(sym_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


FAKE_MINIDUMP_STACKWALK_SCRIPT = r"""#!/usr/bin/env python3
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
            if (
                child.is_dir()
                and child.name.lower() == did_low
                and child not in candidate_bases
            ):
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
                default_trust = "cfi" if has_cfi else ("context" if idx == 0 else "fp")
                trust = fr.get("trust") or default_trust
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
                frame_obj["registers"] = {
                    "rax": "0x0",
                    "rip": frame_obj["offset"],
                    "rsp": "0x000000100000",
                }
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
    crashing_thread_name = (
        threads_out[crashing_idx]["thread_name"] if valid_crashing else "MainThread"
    )

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
"""

_FAKE_DUMP_SYMS_SCRIPT = FAKE_DUMP_SYMS_SCRIPT
_FAKE_MINIDUMP_STACKWALK_SCRIPT = FAKE_MINIDUMP_STACKWALK_SCRIPT
