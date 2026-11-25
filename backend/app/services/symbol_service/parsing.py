"""Path/filename validation and Breakpad .sym header parsing helpers."""

from __future__ import annotations

import io
from collections.abc import Iterable
from pathlib import Path

from app.services.storage import StorageError, StorageManager
from app.services.symbol_service.models import ParsedSymHeader, SymbolConversionError


def _validate_path_segment(value: str, label: str) -> str:
    """Validate that a symbol header field is a single safe path segment."""
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or ".." in value
    ):
        raise SymbolConversionError(f"Invalid {label} path segment: {value!r}")
    return value


def clamp_upload_filename(name: str, max_bytes: int = 240) -> str:
    """Clamp a sanitized filename to <= max_bytes UTF-8 bytes, preserving extension."""
    if len(name.encode("utf-8")) <= max_bytes:
        return name
    suffix = Path(name).suffix
    suffix_bytes = len(suffix.encode("utf-8"))
    if suffix and suffix_bytes < max_bytes:
        stem = name[: len(name) - len(suffix)]
        budget = max_bytes - suffix_bytes
        clamped_stem = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore").rstrip(".")
        if clamped_stem:
            return clamped_stem + suffix
    clamped = name.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore").rstrip(".")
    return clamped or "upload"


def _sanitize_filename(name: str | None, label: str) -> str:
    if not name or not name.strip():
        raise SymbolConversionError(f"Missing {label} filename")
    if (
        name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or ".." in name
    ):
        raise SymbolConversionError(f"Invalid {label} filename: {name!r}")
    return clamp_upload_filename(name.strip(), max_bytes=240)


def _resolve_in_partition(storage: StorageManager, partition: str, rel_path: str) -> Path:
    """Resolve rel_path and enforce strict containment within the named partition."""
    try:
        resolved = storage.resolve_path(rel_path)
    except StorageError as exc:
        raise SymbolConversionError(f"Unsafe {partition} path generated: {rel_path}") from exc
    part_root = storage.partition_path(partition).resolve()
    if not resolved.is_relative_to(part_root) or resolved == part_root:
        raise SymbolConversionError(
            f"Path escapes {partition} partition: {rel_path}"
        )
    return resolved


def parse_sym_header(sym_text: str) -> ParsedSymHeader:
    """Parse MODULE, INFO CODE_ID, and unwind records from Breakpad .sym text."""
    return _parse_sym_records(io.StringIO(sym_text))


def _parse_sym_records(lines: Iterable[str]) -> ParsedSymHeader:
    os_name = ""
    arch = ""
    debug_id = ""
    debug_file = ""
    code_id: str | None = None
    code_file: str | None = None
    has_unwind_records = False
    found_module = False

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        upper_line = line.upper()
        if upper_line.startswith("MODULE "):
            if found_module:
                raise SymbolConversionError("Symbol output contains more than one MODULE")
            parts = line.split(None, 4)
            if len(parts) < 5:
                raise SymbolConversionError(f"Malformed MODULE line: {line!r}")
            _, os_name, arch, raw_debug_id, debug_file = parts
            debug_id = raw_debug_id.strip().upper()
            debug_file = debug_file.strip()
            found_module = True
        elif upper_line.startswith("INFO "):
            parts = line.split(None, 3)
            if len(parts) >= 3 and parts[1].upper() == "CODE_ID":
                code_id = parts[2].strip().upper() or None
                if len(parts) >= 4:
                    code_file = parts[3].strip() or None
        elif upper_line.startswith("STACK CFI ") or upper_line.startswith("STACK WIN "):
            has_unwind_records = True

    if not found_module or not debug_id or not debug_file:
        raise SymbolConversionError("Breakpad .sym output missing valid MODULE header")

    _validate_path_segment(debug_id, "debug_id")
    _validate_path_segment(debug_file, "debug_file")
    if len(debug_id) > 128:
        raise SymbolConversionError("debug_id exceeds maximum length of 128 characters")
    if len(debug_file) > 255:
        raise SymbolConversionError("debug_file exceeds maximum length of 255 characters")
    if code_id is not None:
        _validate_path_segment(code_id, "code_id")
        if len(code_id) > 128:
            raise SymbolConversionError("code_id exceeds maximum length of 128 characters")
    if code_file is not None:
        _validate_path_segment(code_file, "code_file")
        if len(code_file) > 255:
            raise SymbolConversionError("code_file exceeds maximum length of 255 characters")

    stem = Path(debug_file).stem or debug_file
    _validate_path_segment(stem, "stem")
    module_name = (Path(code_file).stem if code_file else stem)[:255]
    return ParsedSymHeader(
        os_name=os_name,
        arch=arch,
        debug_id=debug_id,
        debug_file=debug_file,
        code_id=code_id,
        code_file=code_file,
        module_name=module_name,
        stem=stem,
        has_unwind_records=has_unwind_records,
    )


def _module_identity(header: ParsedSymHeader) -> tuple[str, str, str]:
    return header.os_name.lower(), header.arch.lower(), header.debug_id
