"""Local symbol cache lookup, MSDL negative cache, circuit breaker, and capacity checks."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from app.services.ms_symbol_service.constants import (
    CIRCUIT_COOLDOWN_SECONDS,
    CIRCUIT_FAILURE_THRESHOLD,
    CIRCUIT_STATE_REL,
    MISS_CACHE_DIR_REL,
)
from app.services.ms_symbol_service.identity import _is_safe_segment, normalize_debug_file_name
from app.services.storage import StorageError, StorageManager
from app.services.symbol_service import SymbolConversionError, _parse_sym_records

logger = logging.getLogger(__name__)


def _find_local_sym_in_root(root: Path, debug_file: str, debug_id: str) -> Path | None:
    if not root.exists():
        return None
    base_df = normalize_debug_file_name(debug_file)
    if not _is_safe_segment(base_df) or not _is_safe_segment(debug_id):
        return None
    stem = Path(base_df).stem or base_df
    candidate_leaves = [f"{stem}.sym"]
    full_leaf = f"{base_df}.sym"
    if full_leaf.lower() != candidate_leaves[0].lower():
        candidate_leaves.append(full_leaf)

    upper_did = debug_id.upper()
    for leaf in candidate_leaves:
        exact = root / base_df / upper_did / leaf
        if exact.is_file():
            return exact

    # Case-insensitive fallback for Linux hosts storing Windows PDB names
    df_low = base_df.lower()
    did_low = debug_id.lower()
    sym_lows = {leaf.lower() for leaf in candidate_leaves}
    try:
        for df_entry in root.iterdir():
            if not df_entry.is_dir() or df_entry.name.lower() != df_low:
                continue
            for id_entry in df_entry.iterdir():
                if not id_entry.is_dir() or id_entry.name.lower() != did_low:
                    continue
                for file_entry in id_entry.iterdir():
                    if file_entry.is_file() and file_entry.name.lower() in sym_lows:
                        return file_entry
    except OSError:
        return None
    return None


def _has_local_sym_in_root(root: Path, debug_file: str, debug_id: str) -> bool:
    return _find_local_sym_in_root(root, debug_file, debug_id) is not None


def _sym_file_has_unwind(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return _parse_sym_records(fh).has_unwind_records
    except (OSError, SymbolConversionError):
        return False


def has_cached_symbol_detail(
    storage: StorageManager,
    debug_file: str,
    debug_id: str,
    project_id: uuid.UUID | str | None = None,
) -> tuple[bool, str | None, Path | None]:
    """Return (exists, scope, path) for a Breakpad .sym in project symbols or public_cache."""
    if project_id is not None:
        proj_seg = str(project_id).strip()
        if _is_safe_segment(proj_seg):
            try:
                proj_root = storage.resolve_path(f"symbols/sym/{proj_seg}")
            except StorageError:
                proj_root = None
            if proj_root is not None:
                found_proj = _find_local_sym_in_root(proj_root, debug_file, debug_id)
                if found_proj is not None:
                    return True, "project", found_proj
    pub_root = storage.partition_path("symbols/public_cache")
    found_pub = _find_local_sym_in_root(pub_root, debug_file, debug_id)
    if found_pub is not None:
        return True, "public", found_pub
    return False, None, None


def has_cached_symbol(
    storage: StorageManager,
    debug_file: str,
    debug_id: str,
    project_id: uuid.UUID | str | None = None,
) -> bool:
    """Return True if a Breakpad .sym already exists in project symbols or public_cache."""
    found, _, _ = has_cached_symbol_detail(storage, debug_file, debug_id, project_id=project_id)
    return found


def _miss_cache_rel(debug_file: str, debug_id: str) -> str:
    digest = hashlib.sha256(f"{debug_file.lower()}:{debug_id.upper()}".encode()).hexdigest()[:32]
    return f"{MISS_CACHE_DIR_REL}/{digest}.json"


def get_negative_cache_info(
    storage: StorageManager, debug_file: str, debug_id: str
) -> dict[str, Any] | None:
    """Return active negative-cache payload if present and unexpired, else None."""
    rel = _miss_cache_rel(debug_file, debug_id)
    try:
        path = storage.resolve_path(rel)
    except StorageError:
        return None
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        expires_at = float(payload.get("expires_at", 0.0))
    except (OSError, ValueError, TypeError):
        return None
    if time.time() < expires_at:
        return payload if isinstance(payload, dict) else {"reason": "unknown"}
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def is_negative_cached(storage: StorageManager, debug_file: str, debug_id: str) -> bool:
    """Return True when (debug_file, debug_id) has an unexpired negative-cache entry."""
    return get_negative_cache_info(storage, debug_file, debug_id) is not None


def clear_negative_cache(
    storage: StorageManager,
    debug_file: str | None = None,
    debug_id: str | None = None,
) -> int:
    """Clear either one (debug_file, debug_id) negative-cache entry or the entire miss cache."""
    if debug_file and debug_id:
        rel = _miss_cache_rel(debug_file, debug_id)
        try:
            path = storage.resolve_path(rel)
            if path.is_file():
                path.unlink(missing_ok=True)
                return 1
        except (StorageError, OSError):
            pass
        return 0
    try:
        miss_dir = storage.resolve_path(MISS_CACHE_DIR_REL)
    except StorageError:
        return 0
    if not miss_dir.is_dir():
        return 0
    removed = 0
    try:
        for item in miss_dir.iterdir():
            if item.is_file():
                try:
                    item.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed


def reset_circuit_breaker(storage: StorageManager) -> None:
    """Reset any open MSDL circuit breaker state."""
    _record_circuit_success(storage)


def _write_negative_cache(
    storage: StorageManager,
    debug_file: str,
    debug_id: str,
    reason: str,
    ttl_seconds: int,
) -> None:
    if ttl_seconds <= 0 or storage.is_host_floor_breached():
        return
    rel = _miss_cache_rel(debug_file, debug_id)
    payload = {
        "debug_file": debug_file,
        "debug_id": debug_id.upper(),
        "reason": reason,
        "expires_at": time.time() + ttl_seconds,
    }
    try:
        storage.atomic_write_bytes(rel, json.dumps(payload).encode("utf-8"))
    except (OSError, StorageError):
        logger.debug("Could not persist MSDL negative cache for %s/%s", debug_file, debug_id)


def _read_circuit_state(storage: StorageManager) -> dict[str, Any]:
    try:
        path = storage.resolve_path(CIRCUIT_STATE_REL)
    except StorageError:
        return {"consecutive_failures": 0, "open_until": 0.0}
    if not path.is_file():
        return {"consecutive_failures": 0, "open_until": 0.0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"consecutive_failures": 0, "open_until": 0.0}


def is_circuit_open(storage: StorageManager) -> bool:
    """Return True when MSDL outbound fetches are temporarily paused due to network errors."""
    state = _read_circuit_state(storage)
    try:
        return time.time() < float(state.get("open_until", 0.0))
    except (TypeError, ValueError):
        return False


def _record_circuit_success(storage: StorageManager) -> None:
    state = _read_circuit_state(storage)
    if not state.get("consecutive_failures") and not state.get("open_until"):
        return
    try:
        storage.atomic_write_bytes(
            CIRCUIT_STATE_REL,
            json.dumps({"consecutive_failures": 0, "open_until": 0.0}).encode("utf-8"),
        )
    except (OSError, StorageError):
        pass


def _record_circuit_network_failure(storage: StorageManager, error: str) -> bool:
    """Record a connectivity failure and return True if the circuit is now open."""
    state = _read_circuit_state(storage)
    failures = int(state.get("consecutive_failures") or 0) + 1
    open_until = 0.0
    tripped = failures >= CIRCUIT_FAILURE_THRESHOLD
    if tripped:
        open_until = time.time() + CIRCUIT_COOLDOWN_SECONDS
    payload = {
        "consecutive_failures": failures,
        "open_until": open_until,
        "last_error": error[:200],
    }
    try:
        if not storage.is_host_floor_breached():
            storage.atomic_write_bytes(CIRCUIT_STATE_REL, json.dumps(payload).encode("utf-8"))
    except (OSError, StorageError):
        pass
    return tripped


def _public_cache_has_room(storage: StorageManager, additional_bytes: int = 0) -> bool:
    if storage.is_host_floor_breached():
        return False
    quota = storage.get_partition_quota_bytes("symbols/public_cache")
    if quota <= 0:
        return False
    used, _ = storage.partition_used_bytes("symbols/public_cache")
    return (used + max(0, additional_bytes)) <= quota
