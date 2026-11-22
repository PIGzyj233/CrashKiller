"""MiniDump parsing via rust-minidump (minidump-stackwalk --json) and normalization."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import zstandard as zstd

from app.config import Settings
from app.services.storage import StorageError, StorageManager


class DumpParseError(Exception):
    """Raised when minidump decompression, execution, or JSON parsing fails."""


@dataclass(slots=True)
class ParsedDumpResult:
    crash_reason: str
    crash_address: str | None
    os_version: str | None
    cpu_arch: str | None
    crashing_thread_index: int
    parsed_summary_json: dict[str, Any]
    parsed_stacks_json: dict[str, Any]
    missing_symbols_json: list[dict[str, Any]]


def _cross_platform_basename(path_str: str) -> str:
    """Extract filename from either Windows or POSIX paths regardless of host OS."""
    cleaned = path_str.replace("\\", "/").rstrip("/")
    return cleaned.rsplit("/", 1)[-1] if cleaned else ""


def _normalize_inline(inl: dict[str, Any]) -> dict[str, Any]:
    return {
        "function": inl.get("function"),
        "file": inl.get("file"),
        "line": inl.get("line"),
        "function_offset": inl.get("function_offset"),
        "missing_symbols": bool(inl.get("missing_symbols", False)),
    }


def _normalize_frame(frame: dict[str, Any], idx: int) -> dict[str, Any]:
    frame_index = frame.get("frame")
    if frame_index is None:
        frame_index = frame.get("frame_index")
    if frame_index is None or isinstance(frame_index, bool):
        norm_frame_index = idx
    else:
        try:
            norm_frame_index = int(frame_index)
        except (TypeError, ValueError):
            norm_frame_index = idx

    ip = frame.get("offset")
    if ip is None:
        ip = frame.get("ip")

    inlines = [
        _normalize_inline(inl)
        for inl in (frame.get("inlines") or [])
        if isinstance(inl, dict)
    ]
    raw_regs = frame.get("registers")
    registers = dict(raw_regs) if isinstance(raw_regs, dict) else {}

    return {
        "frame_index": norm_frame_index,
        "ip": ip,
        "module": frame.get("module"),
        "module_offset": frame.get("module_offset"),
        "function": frame.get("function"),
        "function_offset": frame.get("function_offset"),
        "file": frame.get("file"),
        "line": frame.get("line"),
        "trust": frame.get("trust"),
        "missing_symbols": bool(frame.get("missing_symbols", False)),
        "inlines": inlines,
        "registers": registers,
    }


def normalize_stackwalk_json(raw: dict[str, Any]) -> ParsedDumpResult:
    """Normalize rust-minidump JSON output into CrashKiller's canonical schema."""
    if not isinstance(raw, dict):
        raise DumpParseError("minidump-stackwalk JSON root must be an object")

    crash_info = raw.get("crash_info")
    if not isinstance(crash_info, dict):
        crash_info = {}
    crash_reason = str(crash_info.get("type") or "UNKNOWN")[:255]
    raw_addr = crash_info.get("address")
    crash_address = str(raw_addr)[:128] if raw_addr is not None else None

    system_info = raw.get("system_info")
    if not isinstance(system_info, dict):
        system_info = {}
    os_name = str(system_info.get("os") or "").strip()
    os_ver = str(system_info.get("os_ver") or "").strip()
    cpu_arch = system_info.get("cpu") or None
    os_version_raw = " ".join(part for part in (os_name, os_ver) if part).strip() or None
    os_version = os_version_raw[:255] if os_version_raw is not None else None

    crashing_thread_raw = raw.get("crashing_thread")
    if not isinstance(crashing_thread_raw, dict):
        crashing_thread_raw = {}
    raw_threads = raw.get("threads") or []

    threads: list[dict[str, Any]] = []
    for t_idx, thread in enumerate(raw_threads):
        if not isinstance(thread, dict):
            continue
        frames = [
            _normalize_frame(f, f_idx)
            for f_idx, f in enumerate(thread.get("frames") or [])
            if isinstance(f, dict)
        ]
        threads.append(
            {
                "thread_index": t_idx,
                "thread_id": thread.get("thread_id"),
                "thread_name": thread.get("thread_name"),
                "frames": frames,
            }
        )

    raw_ct_idx = crashing_thread_raw.get("threads_index")
    if raw_ct_idx is None:
        raw_ct_idx = crash_info.get("crashing_thread")
    if raw_ct_idx is None or isinstance(raw_ct_idx, bool):
        crashing_thread_index = 0
    else:
        try:
            crashing_thread_index = int(raw_ct_idx)
        except (TypeError, ValueError):
            crashing_thread_index = 0

    crashing_frames_raw = crashing_thread_raw.get("frames") or []
    crashing_frames_norm = [
        _normalize_frame(f, f_idx)
        for f_idx, f in enumerate(crashing_frames_raw)
        if isinstance(f, dict)
    ]

    if not threads and crashing_frames_norm:
        threads.append(
            {
                "thread_index": 0,
                "thread_id": crashing_thread_raw.get("thread_id"),
                "thread_name": crashing_thread_raw.get("thread_name"),
                "frames": crashing_frames_norm,
            }
        )
        crashing_thread_index = 0
    elif crashing_frames_norm and 0 <= crashing_thread_index < len(threads):
        existing_frames = threads[crashing_thread_index]["frames"]
        if not existing_frames:
            threads[crashing_thread_index]["frames"] = crashing_frames_norm
        else:
            for idx, cf in enumerate(crashing_frames_norm):
                if idx < len(existing_frames):
                    if not existing_frames[idx]["registers"] and cf["registers"]:
                        existing_frames[idx]["registers"] = cf["registers"]
                    if not existing_frames[idx]["inlines"] and cf["inlines"]:
                        existing_frames[idx]["inlines"] = cf["inlines"]

    if not (0 <= crashing_thread_index < len(threads)):
        crashing_thread_index = 0

    modules: list[dict[str, Any]] = []
    missing_symbols: list[dict[str, Any]] = []
    seen_missing: set[tuple[str | None, str | None]] = set()

    for mod in raw.get("modules") or []:
        if not isinstance(mod, dict):
            continue
        filename = mod.get("filename")
        extracted_name = _cross_platform_basename(filename) if isinstance(filename, str) else ""
        module_name = extracted_name or (mod.get("debug_file") or "unknown")
        raw_debug_id = mod.get("debug_id")
        debug_id = str(raw_debug_id).upper() if raw_debug_id else None
        debug_file = mod.get("debug_file")
        code_file = mod.get("code_file") or filename
        code_id = mod.get("code_id")
        loaded_symbols = bool(mod.get("loaded_symbols", False))
        corrupt_symbols = bool(mod.get("corrupt_symbols", False))
        raw_missing = mod.get("missing_symbols")
        is_missing = bool(not loaded_symbols if raw_missing is None else raw_missing)

        modules.append(
            {
                "filename": filename,
                "module_name": module_name,
                "base_addr": mod.get("base_addr"),
                "end_addr": mod.get("end_addr"),
                "version": mod.get("version"),
                "debug_file": debug_file,
                "debug_id": debug_id,
                "code_file": code_file,
                "code_id": code_id,
                "loaded_symbols": loaded_symbols,
                "corrupt_symbols": corrupt_symbols,
                "missing_symbols": is_missing,
            }
        )

        if (is_missing or not loaded_symbols) and (debug_file or debug_id):
            key = (debug_file, debug_id)
            if key not in seen_missing:
                seen_missing.add(key)
                missing_symbols.append(
                    {
                        "module_name": module_name,
                        "debug_file": debug_file,
                        "debug_id": debug_id,
                        "code_file": code_file,
                        "code_id": code_id,
                    }
                )

    crashing_frames = (
        threads[crashing_thread_index]["frames"]
        if threads and 0 <= crashing_thread_index < len(threads)
        else []
    )

    parsed_summary_json = {
        "crash_reason": crash_reason,
        "crash_address": crash_address,
        "os_version": os_version,
        "cpu_arch": cpu_arch,
        "crashing_thread_index": crashing_thread_index,
        "thread_count": len(threads),
        "module_count": len(modules),
        "missing_symbol_count": len(missing_symbols),
        "top_frames": crashing_frames[:10],
    }

    parsed_stacks_json = {
        "crashing_thread_index": crashing_thread_index,
        "threads": threads,
        "modules": modules,
    }

    return ParsedDumpResult(
        crash_reason=crash_reason,
        crash_address=crash_address,
        os_version=os_version,
        cpu_arch=cpu_arch,
        crashing_thread_index=crashing_thread_index,
        parsed_summary_json=parsed_summary_json,
        parsed_stacks_json=parsed_stacks_json,
        missing_symbols_json=missing_symbols,
    )


async def parse_dump(
    dump_path: Path | str,
    storage: StorageManager,
    settings: Settings,
    exec_fn: Any = None,
    project_id: uuid.UUID | str | None = None,
) -> ParsedDumpResult:
    """Decompress .dmp.zst if needed, invoke minidump-stackwalk --json, and normalize output."""
    with storage.staging_workspace("dmp") as staging_dir:
        dump_str = str(dump_path)
        if "\x00" in dump_str:
            raise DumpParseError(f"NUL byte in dump path is not allowed: {dump_path!r}")
        if dump_str.endswith(".zst"):
            rel_or_abs = Path(dump_str)
            if rel_or_abs.is_absolute():
                try:
                    rel_path = rel_or_abs.resolve().relative_to(storage.data_dir.resolve())
                except ValueError as exc:
                    raise DumpParseError(
                        f"Absolute dump path outside data_dir: {dump_path}"
                    ) from exc
            else:
                rel_path = rel_or_abs
            staged_dmp = staging_dir / "crash.dmp"
            try:
                await asyncio.to_thread(storage.decompress_dump, rel_path, staged_dmp)
            except (StorageError, zstd.ZstdError, OSError) as exc:
                raise DumpParseError(f"Failed to decompress dump archive: {exc}") from exc
        else:
            candidate = Path(dump_str)
            if not candidate.is_absolute():
                try:
                    candidate = storage.resolve_path(candidate)
                except StorageError as exc:
                    raise DumpParseError(str(exc)) from exc
            if not candidate.is_file():
                raise DumpParseError(f"Minidump file not found: {dump_path}")
            staged_dmp = candidate

        sym_root = storage.partition_path("symbols/sym")
        if project_id is not None:
            proj_seg = str(project_id).strip()
            if (
                not proj_seg
                or proj_seg in {".", ".."}
                or "/" in proj_seg
                or "\\" in proj_seg
                or "\x00" in proj_seg
                or ".." in proj_seg
            ):
                raise DumpParseError(f"Invalid project_id path segment: {project_id!r}")
            try:
                sym_dir = storage.resolve_path(f"symbols/sym/{proj_seg}")
            except StorageError as exc:
                raise DumpParseError(f"Invalid project symbol dir: {exc}") from exc
            sym_dir.mkdir(parents=True, exist_ok=True)
        else:
            sym_dir = sym_root
        cmd = [settings.MINIDUMP_STACKWALK_BIN, "--json"]
        if settings.ENABLE_PUBLIC_SYMBOLS:
            pub_cache = storage.partition_path("symbols/public_cache")
            for url in settings.PUBLIC_SYMBOL_URLS:
                cmd.extend(["--symbols-url", url])
            cmd.extend(["--symbols-cache", str(pub_cache)])
        cmd.extend([str(staged_dmp), str(sym_dir)])

        exec_callable = exec_fn or asyncio.create_subprocess_exec
        try:
            proc = await exec_callable(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise DumpParseError(
                f"minidump-stackwalk binary not found: {settings.MINIDUMP_STACKWALK_BIN}"
            ) from exc
        except OSError as exc:
            raise DumpParseError(f"Failed to start minidump-stackwalk: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=settings.TOOL_TIMEOUT_SECONDS,
            )
        except (TimeoutError, asyncio.CancelledError) as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise DumpParseError(
                f"minidump-stackwalk timed out after {settings.TOOL_TIMEOUT_SECONDS}s"
            ) from exc

        rc = proc.returncode if proc.returncode is not None else -1
        if rc != 0:
            err_msg = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise DumpParseError(
                f"minidump-stackwalk exited {rc}: {err_msg}"
            )

        try:
            raw_json = json.loads((stdout or b"").decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise DumpParseError(f"Invalid JSON from minidump-stackwalk: {exc}") from exc

        return normalize_stackwalk_json(raw_json)


__all__ = [
    "DumpParseError",
    "ParsedDumpResult",
    "normalize_stackwalk_json",
    "parse_dump",
]
