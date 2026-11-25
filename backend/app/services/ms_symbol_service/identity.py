"""Symbol path normalization, URL classification, Windows module checks, and target parsing."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlparse

from app.services.ms_symbol_service.constants import (
    _ABS_DRIVE_PATH_RE,
    _ALL_ZERO_RE,
    _CODE_ID_RE,
    _DEBUG_ID_RE,
    _INVALID_MANUAL_NAME_RE,
    _MANUAL_GUID_RE,
    _WINDOWS_SYSTEM_PATH_RE,
    KNOWN_WINDOWS_MODULE_PREFIXES,
    KNOWN_WINDOWS_SYSTEM_MODULES,
    PORTABLE_AGE,
    SYMSRV_SCHEME_PREFIX,
)
from app.services.ms_symbol_service.models import WindowsSymbolCandidate
from app.services.symbol_service import SymbolConversionError, _validate_path_segment


def _basename(path_str: str) -> str:
    cleaned = path_str.replace("\\", "/").rstrip("/")
    return cleaned.rsplit("/", 1)[-1] if cleaned else ""


def _is_safe_segment(value: str) -> bool:
    try:
        _validate_path_segment(value, "segment")
    except SymbolConversionError:
        return False
    return True


def normalize_debug_file_name(raw_debug_file: str) -> str:
    """Strip clean absolute Windows/POSIX paths to basename while keeping traversal strings raw."""
    cleaned = raw_debug_file.strip()
    if not cleaned or "\x00" in cleaned:
        return cleaned
    if "/" not in cleaned and "\\" not in cleaned:
        return cleaned
    # Only strip clean absolute paths (e.g. C:\Windows\System32\ntdll.pdb) without '..' segments;
    # leave relative/hostile paths (like ../evil.pdb) untouched so _is_safe_segment rejects them.
    is_abs = bool(_ABS_DRIVE_PATH_RE.match(cleaned)) or cleaned.startswith("/")
    if not is_abs:
        return cleaned
    parts = [p for p in cleaned.replace("\\", "/").split("/") if p]
    if any(p == ".." or ".." in p for p in parts):
        return cleaned
    leaf = parts[-1] if parts else ""
    return leaf if _is_safe_segment(leaf) else cleaned


def _is_symsrv_url(raw_url: str) -> tuple[bool, str]:
    """Return (is_symsrv, normalized_http_url) for a configured symbol URL."""
    url = raw_url.strip()
    if not url:
        return False, ""
    if url.lower().startswith(SYMSRV_SCHEME_PREFIX):
        stripped = url[len(SYMSRV_SCHEME_PREFIX) :].strip()
        return True, stripped

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower().rstrip("/")
    if host == "msdl.microsoft.com":
        return True, url
    if host.endswith(".microsoft.com") and "/symbols" in path:
        return True, url
    if path.endswith("/download/symbols") or path.endswith("/symsrv"):
        return True, url
    return False, url


def split_public_symbol_urls(urls: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split PUBLIC_SYMBOL_URLS into (breakpad_sym_urls, symsrv_pdb_urls)."""
    breakpad_urls: list[str] = []
    symsrv_urls: list[str] = []
    seen_bp: set[str] = set()
    seen_ss: set[str] = set()

    for raw in urls:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip()
        if not cleaned:
            continue
        is_ss, normalized = _is_symsrv_url(cleaned)
        if not normalized:
            continue
        parsed = urlparse(normalized)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            continue
        if is_ss:
            key = normalized.rstrip("/").lower()
            if key not in seen_ss:
                seen_ss.add(key)
                symsrv_urls.append(normalized)
        else:
            key = normalized.rstrip("/").lower()
            if key not in seen_bp:
                seen_bp.add(key)
                breakpad_urls.append(normalized)

    return breakpad_urls, symsrv_urls


def is_windows_system_module(
    *,
    filename: str | None,
    code_file: str | None,
    module_name: str | None,
    debug_file: str | None,
) -> bool:
    """Return True only when a module clearly belongs to Windows OS or Microsoft runtimes."""
    if not debug_file or not debug_file.strip().lower().endswith(".pdb"):
        return False

    for raw_path in (filename, code_file):
        if not raw_path:
            continue
        norm = raw_path.strip().replace("\\", "/")
        norm_low = norm.lower()
        if "/driverstore/filerepository/" in norm_low:
            continue
        if _WINDOWS_SYSTEM_PATH_RE.match(norm):
            return True

    names: list[str] = []
    for candidate_name in (module_name, _basename(filename or ""), _basename(code_file or "")):
        if candidate_name:
            low = candidate_name.strip().lower()
            if low:
                names.append(low)
    pdb_base = _basename(debug_file).strip().lower()
    if pdb_base.endswith(".pdb") and len(pdb_base) > 4:
        names.append(f"{pdb_base[:-4]}.dll")

    for name in names:
        if name in KNOWN_WINDOWS_SYSTEM_MODULES:
            return True
        if any(name.startswith(prefix) for prefix in KNOWN_WINDOWS_MODULE_PREFIXES):
            return True

    return False


def _is_valid_pdb_identity(debug_file: str, debug_id: str) -> bool:
    if not debug_file or debug_file != debug_file.strip():
        return False
    if len(debug_file) <= 4 or not debug_file.lower().endswith(".pdb"):
        return False
    if (
        len(debug_file) > 255
        or not _is_safe_segment(debug_file)
        or _INVALID_MANUAL_NAME_RE.search(debug_file)
    ):
        return False
    if not debug_id or not _DEBUG_ID_RE.match(debug_id):
        return False
    if _ALL_ZERO_RE.match(debug_id) or _ALL_ZERO_RE.match(debug_id[:32]):
        return False
    return True


def _is_valid_pe_identity(code_file: str | None, code_id: str | None) -> tuple[str, str] | None:
    if not code_file or not code_id:
        return None
    base = _basename(code_file).strip()
    cid = code_id.strip().upper()
    if not base or len(base) > 255 or not _is_safe_segment(base):
        return None
    if not (base.lower().endswith(".dll") or base.lower().endswith(".exe")):
        return None
    if not _CODE_ID_RE.match(cid) or _ALL_ZERO_RE.match(cid):
        return None
    return base, cid


def _public_cache_sym_rel(debug_file: str, debug_id: str) -> str:
    safe_df = _validate_path_segment(debug_file, "debug_file")
    safe_did = _validate_path_segment(debug_id.upper(), "debug_id")
    stem = _validate_path_segment(Path(safe_df).stem or safe_df, "stem")
    return f"symbols/public_cache/{safe_df}/{safe_did}/{stem}.sym"


def _is_microsoft_symsrv_url(raw_url: str) -> bool:
    parsed = urlparse(raw_url.strip())
    host = (parsed.hostname or "").lower()
    return host == "msdl.microsoft.com" or host.endswith(".microsoft.com")


def _normalize_pe_code_ids(code_id: str) -> list[str]:
    """Return SymSrv PE key candidates ordered by MSDL canonical format first.

    Microsoft's SymSrv expects `{timestamp:08X}{image_size:x}` (8 uppercase hex chars
    for timestamp + lowercase hex image_size without leading zeros).
    """
    cid = code_id.strip()
    if not cid:
        return []
    upper = cid.upper()
    if re.fullmatch(r"[0-9A-Fa-f]{9,16}", cid):
        size_val = int(cid[8:], 16)
        if size_val > 0:
            canonical = f"{cid[:8].upper()}{size_val:x}"
            if canonical != upper:
                return [canonical, upper]
            return [canonical]
    return [upper]


def _parse_age_value(age: str | int) -> int:
    if isinstance(age, bool):
        raise ValueError("Invalid PDB age")
    if isinstance(age, int):
        val = age
    else:
        cleaned = str(age).strip()
        if not cleaned:
            raise ValueError("PDB age must not be empty")
        val = int(cleaned, 16 if cleaned.lower().startswith("0x") else 10)
    if not 0 <= val <= PORTABLE_AGE:
        raise ValueError("PDB age out of range")
    return val


def parse_manual_ms_target(
    module_or_pdb: str,
    debug_id_or_guid: str,
    *,
    age: str | int | None = None,
    code_file: str | None = None,
    code_id: str | None = None,
) -> WindowsSymbolCandidate:
    """Parse a user-supplied module/PDB name and Debug ID / GUID into a sync candidate.

    Accepts inputs like `GraphicsCapture.dll` + `90D28435EAAB109449C47402C53C42D31`
    or `GraphicsCapture.pdb` + dashed GUID + optional age.
    """
    raw_name = _basename(module_or_pdb.strip())
    if not raw_name or _INVALID_MANUAL_NAME_RE.search(raw_name):
        raise ValueError("Invalid module or PDB filename")

    low_name = raw_name.lower()
    if low_name.endswith(".pdb"):
        stem = raw_name[:-4]
        debug_file = raw_name
        derived_code_file = f"{stem}.dll" if stem else None
        module_name = derived_code_file or raw_name
    elif low_name.endswith((".dll", ".exe")):
        stem = raw_name[:-4]
        debug_file = f"{stem}.pdb"
        derived_code_file = raw_name
        module_name = raw_name
    else:
        stem = raw_name
        debug_file = f"{stem}.pdb"
        derived_code_file = f"{stem}.dll"
        module_name = derived_code_file

    if not stem or not _is_safe_segment(debug_file):
        raise ValueError("Invalid module or PDB filename")

    match = _MANUAL_GUID_RE.fullmatch(debug_id_or_guid.strip())
    if not match:
        raise ValueError(
            "debug_id must be a 32-hex/dashed GUID optionally followed by a hex age"
        )
    parsed_guid = uuid.UUID(match.group(1))
    if parsed_guid.int == 0:
        raise ValueError("GUID must not be all zeros")

    inline_age_hex = match.group(2)
    inline_age = int(inline_age_hex, 16) if inline_age_hex else None
    explicit_age = _parse_age_value(age) if age is not None and str(age).strip() != "" else None
    if inline_age is not None and explicit_age is not None and inline_age != explicit_age:
        raise ValueError("Provided age conflicts with the age suffix in debug_id")
    final_age = (
        inline_age
        if inline_age is not None
        else (explicit_age if explicit_age is not None else 1)
    )
    debug_id = f"{parsed_guid.hex.upper()}{final_age:X}"

    resolved_code_file = (
        _basename(code_file.strip())
        if code_file and code_file.strip()
        else derived_code_file
    )
    if resolved_code_file and (
        _INVALID_MANUAL_NAME_RE.search(resolved_code_file)
        or not _is_safe_segment(resolved_code_file)
    ):
        raise ValueError("Invalid code_file name")

    resolved_code_id: str | None = None
    if code_id is not None and code_id.strip():
        cid = code_id.strip()
        if not _CODE_ID_RE.match(cid) or _ALL_ZERO_RE.match(cid):
            raise ValueError("Invalid code_id (expected hex timestamp + image size)")
        resolved_code_id = cid

    return WindowsSymbolCandidate(
        module_name=module_name,
        debug_file=debug_file,
        debug_id=debug_id,
        code_file=resolved_code_file,
        code_id=resolved_code_id,
        filename=resolved_code_file,
    )
