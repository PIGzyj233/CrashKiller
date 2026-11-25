"""Stack-referenced missing Windows system module selection for MSDL import."""

from __future__ import annotations

import uuid
from typing import Any

from app.config import Settings
from app.services.ms_symbol_service.cache import has_cached_symbol, is_negative_cached
from app.services.ms_symbol_service.identity import (
    _basename,
    _is_valid_pdb_identity,
    is_windows_system_module,
)
from app.services.ms_symbol_service.models import WindowsSymbolCandidate
from app.services.storage import StorageManager


def select_windows_system_candidates(
    *,
    os_version: str | None,
    parsed_stacks_json: dict[str, Any] | None,
    missing_symbols_json: list[dict[str, Any]] | None,
    storage: StorageManager,
    settings: Settings,
    project_id: uuid.UUID | str | None = None,
) -> list[WindowsSymbolCandidate]:
    """Select stack-referenced missing Windows system modules eligible for MSDL import."""
    if os_version and "windows" not in os_version.lower():
        return []
    if not parsed_stacks_json or not isinstance(parsed_stacks_json, dict):
        return []

    max_modules = max(0, int(settings.MS_SYMBOL_MAX_MODULES_PER_CRASH))
    if max_modules == 0:
        return []

    modules = parsed_stacks_json.get("modules") or []
    missing_by_mod: dict[str, dict[str, Any]] = {}

    for mod in modules:
        if not isinstance(mod, dict):
            continue
        loaded = bool(mod.get("loaded_symbols", False))
        is_missing = mod.get("missing_symbols")
        if loaded and is_missing is not True:
            continue
        debug_file = str(mod.get("debug_file") or "").strip()
        debug_id = str(mod.get("debug_id") or "").strip().upper()
        if not _is_valid_pdb_identity(debug_file, debug_id):
            continue
        mod_name = str(mod.get("module_name") or _basename(str(mod.get("filename") or ""))).strip()
        if not mod_name:
            continue
        missing_by_mod.setdefault(
            mod_name.lower(),
            {
                "module_name": mod_name,
                "filename": mod.get("filename"),
                "debug_file": debug_file,
                "debug_id": debug_id,
                "code_file": mod.get("code_file") or mod.get("filename"),
                "code_id": mod.get("code_id"),
            },
        )

    for entry in missing_symbols_json or []:
        if not isinstance(entry, dict):
            continue
        debug_file = str(entry.get("debug_file") or "").strip()
        debug_id = str(entry.get("debug_id") or "").strip().upper()
        if not _is_valid_pdb_identity(debug_file, debug_id):
            continue
        mod_name = str(entry.get("module_name") or "").strip()
        if not mod_name:
            continue
        missing_by_mod.setdefault(
            mod_name.lower(),
            {
                "module_name": mod_name,
                "filename": entry.get("code_file"),
                "debug_file": debug_file,
                "debug_id": debug_id,
                "code_file": entry.get("code_file"),
                "code_id": entry.get("code_id"),
            },
        )

    if not missing_by_mod:
        return []

    threads = parsed_stacks_json.get("threads") or []
    crashing_idx = parsed_stacks_json.get("crashing_thread_index", 0)
    ordered_threads: list[dict[str, Any]] = []
    if isinstance(crashing_idx, int) and 0 <= crashing_idx < len(threads):
        ct = threads[crashing_idx]
        if isinstance(ct, dict):
            ordered_threads.append(ct)
    for idx, thread in enumerate(threads):
        if idx == crashing_idx or not isinstance(thread, dict):
            continue
        ordered_threads.append(thread)

    candidates: list[WindowsSymbolCandidate] = []
    seen_keys: set[tuple[str, str]] = set()

    for thread in ordered_threads:
        for frame in thread.get("frames") or []:
            if not isinstance(frame, dict):
                continue
            raw_mod = frame.get("module")
            if not isinstance(raw_mod, str) or not raw_mod.strip():
                continue
            mod_key = _basename(raw_mod.strip()).lower()
            info = missing_by_mod.get(mod_key)
            if info is None:
                continue

            debug_file = str(info["debug_file"])
            debug_id = str(info["debug_id"]).upper()
            dedup_key = (debug_file.lower(), debug_id)
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            filename_val = str(info["filename"]).strip() if info.get("filename") else None
            code_file_val = str(info["code_file"]).strip() if info.get("code_file") else None
            code_id_val = str(info["code_id"]).strip().upper() if info.get("code_id") else None

            if not is_windows_system_module(
                filename=filename_val,
                code_file=code_file_val,
                module_name=str(info["module_name"]),
                debug_file=debug_file,
            ):
                continue

            if has_cached_symbol(storage, debug_file, debug_id, project_id=project_id):
                continue
            if is_negative_cached(storage, debug_file, debug_id):
                continue

            candidates.append(
                WindowsSymbolCandidate(
                    module_name=str(info["module_name"]),
                    debug_file=debug_file,
                    debug_id=debug_id,
                    code_file=code_file_val,
                    code_id=code_id_val,
                    filename=filename_val,
                )
            )
            if len(candidates) >= max_modules:
                return candidates

    return candidates
