"""Breakpad symbol conversion (dump_syms), deduplication, raw archiving, and SymSrv."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import shutil
import struct
import sys
import tempfile
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import zstandard as zstd
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.db.models import Symbol
from app.services.storage import StorageCapacityError, StorageError, StorageManager

MAX_SYMBOL_OUTPUT_BYTES = 128 * 1024**2
MAX_SYMBOL_TOOL_MEMORY_BYTES = 1536 * 1024**2


class SymbolConversionError(Exception):
    """Raised when dump_syms fails or produces invalid Breakpad .sym output."""


@dataclass(slots=True)
class ParsedSymHeader:
    os_name: str
    arch: str
    debug_id: str
    debug_file: str
    code_id: str | None
    code_file: str | None
    module_name: str
    stem: str
    has_unwind_records: bool


@dataclass(slots=True)
class SymbolIngestResult:
    symbol: Symbol
    deduplicated: bool
    upgraded: bool


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


async def _run_subprocess(
    cmd: list[str],
    timeout_seconds: int,
    exec_fn: Any = None,
) -> tuple[int, bytes, bytes]:
    if exec_fn is None:
        native_cmd = cmd
        if sys.platform == "linux":
            native_cmd = [
                "prlimit",
                f"--as={MAX_SYMBOL_TOOL_MEMORY_BYTES}",
                f"--fsize={MAX_SYMBOL_OUTPUT_BYTES}",
                "--",
                *cmd,
            ]
        with (
            tempfile.TemporaryFile(dir=Path(cmd[1]).parent) as stdout_file,
            tempfile.TemporaryFile(dir=Path(cmd[1]).parent) as stderr_file,
        ):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *native_cmd, stdout=stdout_file, stderr=stderr_file
                )
            except OSError as exc:
                raise SymbolConversionError(f"Failed to start {cmd[0]}: {exc}") from exc
            try:
                rc = await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
            except (TimeoutError, asyncio.CancelledError) as exc:
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise SymbolConversionError(
                    f"dump_syms timed out after {timeout_seconds}s"
                ) from exc
            if os.fstat(stdout_file.fileno()).st_size >= MAX_SYMBOL_OUTPUT_BYTES:
                raise SymbolConversionError("dump_syms output exceeds the 128 MiB conversion limit")
            stdout_file.seek(0)
            stderr_file.seek(0)
            return rc, stdout_file.read(), stderr_file.read(16384)

    try:
        proc = await exec_fn(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise SymbolConversionError(f"Symbol tool binary not found: {cmd[0]}") from exc
    except OSError as exc:
        raise SymbolConversionError(f"Failed to start {cmd[0]}: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_seconds,
        )
    except (TimeoutError, asyncio.CancelledError) as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise SymbolConversionError(
            f"dump_syms timed out after {timeout_seconds}s: {' '.join(cmd)}"
        ) from exc

    rc = proc.returncode if proc.returncode is not None else -1
    return rc, stdout or b"", stderr or b""


def _module_identity(header: ParsedSymHeader) -> tuple[str, str, str]:
    return header.os_name.lower(), header.arch.lower(), header.debug_id


def _pe_debug_identity(path: Path) -> tuple[str, str] | None:
    """Read the PE CodeView GUID and age without loading the executable into memory."""
    with path.open("rb") as stream:
        dos_header = stream.read(64)
        if len(dos_header) != 64 or dos_header[:2] != b"MZ":
            return None
        stream.seek(struct.unpack_from("<I", dos_header, 60)[0])
        coff_header = stream.read(24)
        if len(coff_header) != 24 or coff_header[:4] != b"PE\0\0":
            return None
        section_count, timestamp = struct.unpack_from("<HI", coff_header, 6)
        optional_size = struct.unpack_from("<H", coff_header, 20)[0]
        optional_header = stream.read(optional_size)
        if optional_header[:2] == b"\x0b\x01":
            directories_offset = 96
        elif optional_header[:2] == b"\x0b\x02":
            directories_offset = 112
        else:
            return None
        debug_offset = directories_offset + 6 * 8
        if len(optional_header) < debug_offset + 8:
            return None
        directory_count = struct.unpack_from("<I", optional_header, directories_offset - 4)[0]
        if directory_count <= 6:
            return None
        debug_rva, debug_size = struct.unpack_from("<II", optional_header, debug_offset)
        if not debug_rva or not 28 <= debug_size <= 65536:
            return None
        image_size, header_size = struct.unpack_from("<II", optional_header, 56)
        debug_position = debug_rva if debug_rva + debug_size <= header_size else None
        if debug_position is None:
            for _ in range(section_count):
                section = stream.read(40)
                if len(section) < 40:
                    return None
                section_rva, file_size, file_offset = struct.unpack_from("<III", section, 12)
                delta = debug_rva - section_rva
                if 0 <= delta and delta + debug_size <= file_size:
                    debug_position = file_offset + delta
                    break
        if debug_position is None:
            return None
        stream.seek(debug_position)
        table = stream.read(debug_size)
        for offset in range(0, len(table) - 27, 28):
            record_type, record_size, _, record_position = struct.unpack_from(
                "<IIII", table, offset + 12
            )
            if record_type != 2 or record_size < 24:
                continue
            stream.seek(record_position)
            record = stream.read(24)
            if len(record) == 24 and record[:4] == b"RSDS":
                guid = uuid.UUID(bytes_le=record[4:20]).hex.upper()
                age = struct.unpack_from("<I", record, 20)[0]
                return f"{guid}{age:X}", f"{timestamp:08X}{image_size:X}"
    return None


async def run_dump_syms(
    pdb_path: Path | None,
    pe_path: Path | None,
    settings: Settings,
    exec_fn: Any = None,
) -> tuple[bytes, bool]:
    """Run dump_syms with [pe, pdb] and automatic [pdb] fallback, returning (sym_bytes, pe_used)."""
    if pdb_path is None and pe_path is None:
        raise SymbolConversionError("At least one of pdb_path or pe_path must be provided")

    timeout = settings.TOOL_TIMEOUT_SECONDS
    bin_path = settings.DUMP_SYMS_BIN

    async def convert(*inputs: Path) -> tuple[int, bytes, bytes]:
        return await _run_subprocess(
            [bin_path, *(str(path) for path in inputs)],
            timeout_seconds=timeout,
            exec_fn=exec_fn,
        )

    if pe_path is not None and pdb_path is not None:
        # A PE conversion may automatically consume a same-directory PDB.
        # Compare an isolated PDB conversion with the PE's own build identifier.
        pe_identity = _pe_debug_identity(pe_path)
        with tempfile.TemporaryDirectory(prefix="probe_", dir=pe_path.parent) as directory:
            probes = Path(directory)
            pdb_probe = probes / pdb_path.name
            os.link(pdb_path, pdb_probe)
            pdb_rc, pdb_sym, pdb_err = await convert(pdb_probe)
            if pdb_rc != 0:
                detail = pdb_err.decode("utf-8", errors="replace").strip()
                raise SymbolConversionError(f"dump_syms could not read the PDB: {detail or pdb_rc}")
            pdb_header = parse_sym_header(pdb_sym.decode("utf-8", errors="replace"))
            if pe_identity is None:
                pe_dir = probes / "pe"
                pe_dir.mkdir()
                pe_probe = pe_dir / pe_path.name
                os.link(pe_path, pe_probe)
                pe_rc, pe_sym, _ = await convert(pe_probe)
                if pe_rc != 0 or b"MODULE " not in pe_sym:
                    return pdb_sym, False
                pe_header = parse_sym_header(pe_sym.decode("utf-8", errors="replace"))
                if _module_identity(pe_header) != _module_identity(pdb_header):
                    raise SymbolConversionError("PDB and PE identify different builds")
                expected_code_id = pe_header.code_id
                del pe_sym
            else:
                expected_debug_id, expected_code_id = pe_identity
                if expected_debug_id != pdb_header.debug_id:
                    raise SymbolConversionError("PDB and PE identify different builds")

        rc, stdout, _ = await convert(pe_path, pdb_path)
        if rc != 0 or b"MODULE " not in stdout:
            return pdb_sym, False
        header = parse_sym_header(stdout.decode("utf-8", errors="replace"))
        if _module_identity(header) != _module_identity(pdb_header):
            raise SymbolConversionError("Combined symbols do not identify the uploaded build")
        if expected_code_id and header.code_id != expected_code_id:
            raise SymbolConversionError("Combined symbols do not identify the uploaded PE")
        return stdout, True

    target = pe_path if pe_path is not None else pdb_path
    assert target is not None
    rc, stdout, stderr = await convert(target)
    if rc == 0 and b"MODULE " in stdout:
        return stdout, pe_path is not None

    err_msg = stderr.decode("utf-8", errors="replace").strip()
    raise SymbolConversionError(f"dump_syms failed: {err_msg or f'exit {rc}'}")


def _input_size(data: bytes | Path | BinaryIO | None) -> int:
    if data is None:
        return 0
    if isinstance(data, bytes):
        return len(data)
    if isinstance(data, Path):
        return data.stat().st_size
    size = data.seek(0, os.SEEK_END)
    data.seek(0)
    return size


def _stage_input(
    staging_dir: Path,
    filename: str | None,
    data_or_path: bytes | Path | BinaryIO | None,
    label: str,
) -> Path | None:
    if data_or_path is None:
        return None
    if isinstance(data_or_path, Path):
        if not data_or_path.exists():
            raise SymbolConversionError(f"{label} file does not exist: {data_or_path}")
        safe_name = _sanitize_filename(filename or data_or_path.name, label)
        dest = staging_dir / safe_name
        shutil.copy2(data_or_path, dest)
        return dest

    safe_name = _sanitize_filename(filename, label)
    dest = staging_dir / safe_name
    if isinstance(data_or_path, bytes):
        dest.write_bytes(data_or_path)
    else:
        data_or_path.seek(0)
        with dest.open("wb") as output:
            shutil.copyfileobj(data_or_path, output, length=1024**2)
    return dest


def _build_raw_archive_paths(
    storage: StorageManager,
    header: ParsedSymHeader,
    pdb_path: Path | None,
    pe_path: Path | None,
) -> tuple[str | None, str | None]:
    """Build and strictly validate symbols/raw archive relative paths."""
    rel_pdb: str | None = None
    rel_pe: str | None = None
    if pdb_path is not None:
        pdb_name = header.debug_file or pdb_path.name
        if len(f"{pdb_name}.zst".encode()) > 255:
            pdb_name = clamp_upload_filename(pdb_name, max_bytes=240)
        _validate_path_segment(pdb_name, "pdb_name")
        rel_pdb = f"symbols/raw/{header.debug_id}/{pdb_name}.zst"
        _resolve_in_partition(storage, "symbols/raw", rel_pdb)
    if pe_path is not None and header.code_id:
        pe_name = header.code_file or pe_path.name
        if len(f"{pe_name}.zst".encode()) > 255:
            pe_name = clamp_upload_filename(pe_name, max_bytes=240)
        _validate_path_segment(pe_name, "pe_name")
        rel_pe = f"symbols/raw/{header.code_id}/{pe_name}.zst"
        _resolve_in_partition(storage, "symbols/raw", rel_pe)
    return rel_pdb, rel_pe


async def _archive_raw_files(
    session: AsyncSession,
    storage: StorageManager,
    header: ParsedSymHeader,
    pdb_path: Path | None,
    pe_path: Path | None,
) -> tuple[str | None, int]:
    """Compress raw PDB/PE files into symbols/raw if safety floor permits."""
    rel_pdb, rel_pe = _build_raw_archive_paths(storage, header, pdb_path, pe_path)
    if storage.is_host_floor_breached():
        return None, 0
    # Raw archives are optional. Allow enough headroom for incompressible input.
    projected = 0
    for source, rel in ((pdb_path, rel_pdb), (pe_path, rel_pe)):
        if source is None or rel is None:
            continue
        size = source.stat().st_size
        target = storage.resolve_path(rel)
        old_size = target.stat().st_size if target.is_file() else 0
        projected += max(0, size + max(65536, size // 100) - old_size)
    try:
        await storage.require_capacity(session, {"symbols/raw": projected})
    except StorageCapacityError:
        return None, 0

    primary_rel: str | None = None
    total_raw_size = 0

    if pdb_path is not None and rel_pdb is not None:
        try:
            archived = await asyncio.to_thread(storage.compress_raw_symbol, pdb_path, rel_pdb)
        except StorageError as exc:
            raise SymbolConversionError(f"Failed to archive raw PDB: {exc}") from exc
        if archived is not None:
            primary_rel, sz = archived
            total_raw_size += sz

    if pe_path is not None and rel_pe is not None:
        try:
            archived = await asyncio.to_thread(storage.compress_raw_symbol, pe_path, rel_pe)
        except StorageError as exc:
            raise SymbolConversionError(f"Failed to archive raw PE: {exc}") from exc
        if archived is not None:
            rel_pe_out, sz = archived
            if primary_rel is None:
                primary_rel = rel_pe_out
            total_raw_size += sz

    return primary_rel, total_raw_size


async def ingest_symbol(
    session: AsyncSession,
    storage: StorageManager,
    settings: Settings,
    project_id: uuid.UUID,
    app_version: str,
    pdb_filename: str | None = None,
    pdb_bytes_or_path: bytes | Path | BinaryIO | None = None,
    pe_filename: str | None = None,
    pe_bytes_or_path: bytes | Path | BinaryIO | None = None,
    exec_fn: Any = None,
) -> SymbolIngestResult:
    """Convert PDB/PE via dump_syms, deduplicate/upgrade in DB, and archive raw symbols."""
    if pdb_bytes_or_path is None and pe_bytes_or_path is None:
        raise SymbolConversionError("At least one of PDB or PE file must be provided")

    async with storage.shared_lock("symbol-conversion"):
        with storage.staging_workspace("sym") as staging_dir:
            async with storage.shared_lock("capacity"):
                incoming = _input_size(pdb_bytes_or_path) + _input_size(pe_bytes_or_path)
                await storage.require_capacity(session, {"staging": incoming})
                staged_pdb = await asyncio.to_thread(
                    _stage_input, staging_dir, pdb_filename, pdb_bytes_or_path, "pdb"
                )
                if staged_pdb is not None and pe_filename == staged_pdb.name:
                    raise SymbolConversionError("PDB and PE must have distinct filenames")
                staged_pe = await asyncio.to_thread(
                    _stage_input, staging_dir, pe_filename, pe_bytes_or_path, "pe"
                )

            sym_bytes, pe_used = await run_dump_syms(
                pdb_path=staged_pdb,
                pe_path=staged_pe,
                settings=settings,
                exec_fn=exec_fn,
            )
            header = parse_sym_header(sym_bytes.decode("utf-8", errors="replace"))
            async with storage.shared_lock("capacity"):
                return await _store_symbol(
                    session,
                    storage,
                    settings,
                    project_id,
                    app_version,
                    staged_pdb,
                    staged_pe if pe_used and header.has_unwind_records else None,
                    sym_bytes,
                    header,
                )


async def _store_symbol(
    session: AsyncSession,
    storage: StorageManager,
    settings: Settings,
    project_id: uuid.UUID,
    app_version: str,
    staged_pdb: Path | None,
    staged_pe: Path | None,
    sym_bytes: bytes,
    header: ParsedSymHeader,
) -> SymbolIngestResult:
    has_pe_unwind = header.has_unwind_records
    project_seg = _validate_path_segment(str(project_id), "project_id")
    rel_sym_path = (
        f"symbols/sym/{project_seg}/{header.debug_file}/{header.debug_id}/{header.stem}.sym"
    )
    abs_sym_path = _resolve_in_partition(storage, "symbols/sym", rel_sym_path)
    _build_raw_archive_paths(storage, header, staged_pdb, staged_pe)

    existing_stmt = select(Symbol).where(
        and_(
            Symbol.project_id == project_id,
            Symbol.debug_file == header.debug_file,
            Symbol.debug_id == header.debug_id,
        )
    )
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()
    now = utcnow()

    if existing is None:
        previous_size = abs_sym_path.stat().st_size if abs_sym_path.is_file() else 0
        await storage.require_capacity(session, {"symbols/sym": len(sym_bytes) - previous_size})
        sym_row = Symbol(
            project_id=project_id,
            app_version=app_version,
            module_name=header.module_name,
            debug_file=header.debug_file,
            debug_id=header.debug_id,
            code_file=header.code_file,
            code_id=header.code_id,
            has_pe_unwind=has_pe_unwind,
            sym_path=rel_sym_path,
            sym_size=len(sym_bytes),
            raw_archive_path=None,
            raw_size=0,
            last_used_at=now,
            created_at=now,
        )
        try:
            async with session.begin_nested():
                session.add(sym_row)
                await session.flush()
        except IntegrityError:
            existing = (await session.execute(existing_stmt)).scalar_one()
        else:
            await asyncio.to_thread(storage.atomic_write_bytes, rel_sym_path, sym_bytes)
            if settings.ARCHIVE_RAW_SYMBOLS:
                raw_rel, raw_sz = await _archive_raw_files(
                    session,
                    storage,
                    header,
                    staged_pdb,
                    staged_pe,
                )
                sym_row.raw_archive_path = raw_rel
                sym_row.raw_size = raw_sz
            await session.commit()
            await session.refresh(sym_row)
            return SymbolIngestResult(symbol=sym_row, deduplicated=False, upgraded=False)

    assert existing is not None
    should_upgrade = has_pe_unwind and not existing.has_pe_unwind
    if staged_pe is not None and not should_upgrade:
        try:
            with abs_sym_path.open(encoding="utf-8", errors="replace") as current:
                stored_header = _parse_sym_records(current)
            should_upgrade = (
                not stored_header.has_unwind_records
                or stored_header.code_id != header.code_id
            )
        except (FileNotFoundError, SymbolConversionError):
            should_upgrade = True
    if should_upgrade:
        previous_size = abs_sym_path.stat().st_size if abs_sym_path.is_file() else 0
        await storage.require_capacity(session, {"symbols/sym": len(sym_bytes) - previous_size})
        await asyncio.to_thread(storage.atomic_write_bytes, rel_sym_path, sym_bytes)
        existing.app_version = app_version
        existing.module_name = header.module_name
        existing.code_file = header.code_file or existing.code_file
        existing.code_id = header.code_id or existing.code_id
        existing.has_pe_unwind = True
        existing.sym_path = rel_sym_path
        existing.sym_size = len(sym_bytes)
        if settings.ARCHIVE_RAW_SYMBOLS:
            raw_rel, raw_sz = await _archive_raw_files(
                session, storage, header, staged_pdb, staged_pe
            )
            if raw_rel is not None:
                if staged_pdb is None and existing.raw_archive_path:
                    if raw_rel != existing.raw_archive_path:
                        existing.raw_size = (existing.raw_size or 0) + raw_sz
                else:
                    existing.raw_archive_path = raw_rel
                    existing.raw_size = raw_sz
        existing.last_used_at = now
        await session.commit()
        await session.refresh(existing)
        return SymbolIngestResult(symbol=existing, deduplicated=True, upgraded=True)

    if not abs_sym_path.exists():
        await storage.require_capacity(session, {"symbols/sym": len(sym_bytes)})
        await asyncio.to_thread(storage.atomic_write_bytes, rel_sym_path, sym_bytes)
        existing.sym_size = len(sym_bytes)
    existing.last_used_at = now
    await session.commit()
    await session.refresh(existing)
    return SymbolIngestResult(symbol=existing, deduplicated=True, upgraded=False)


def _clear_msdl_negative_cache(storage: StorageManager, debug_file: str, debug_id: str) -> None:
    """Remove any MSDL negative-cache entry once a public symbol is available."""
    digest = hashlib.sha256(f"{debug_file.lower()}:{debug_id.upper()}".encode()).hexdigest()[:32]
    rel = f"symbols/public_cache/_msdl_miss/{digest}.json"
    try:
        path = storage.resolve_path(rel)
        path.unlink(missing_ok=True)
    except (StorageError, OSError):
        pass


def _unlink_public_sym_if_distinct(
    storage: StorageManager, old_rel: str | None, new_rel: str
) -> None:
    if not old_rel or old_rel == new_rel:
        return
    try:
        old_abs = _resolve_in_partition(storage, "symbols/public_cache", old_rel)
        old_abs.unlink(missing_ok=True)
    except (SymbolConversionError, OSError):
        pass


async def store_public_symbol(
    session: AsyncSession,
    storage: StorageManager,
    settings: Settings,
    *,
    sym_bytes: bytes,
    header: ParsedSymHeader,
    source: str = "upload",
    staged_pdb: Path | None = None,
    staged_pe: Path | None = None,
    acquire_capacity_lock: bool = True,
) -> SymbolIngestResult:
    """Persist a global project-less, version-less symbol into symbols/public_cache and DB."""

    async def _persist() -> SymbolIngestResult:
        has_pe_unwind = header.has_unwind_records
        rel_sym_path = (
            f"symbols/public_cache/{header.debug_file}/{header.debug_id}/{header.stem}.sym"
        )
        abs_sym_path = _resolve_in_partition(storage, "symbols/public_cache", rel_sym_path)
        _build_raw_archive_paths(storage, header, staged_pdb, staged_pe)

        existing_stmt = select(Symbol).where(
            and_(
                Symbol.project_id.is_(None),
                func.lower(Symbol.debug_file) == header.debug_file.lower(),
                func.upper(Symbol.debug_id) == header.debug_id.upper(),
            )
        )
        existing = (await session.execute(existing_stmt)).scalars().first()
        now = utcnow()

        if existing is None:
            previous_size = abs_sym_path.stat().st_size if abs_sym_path.is_file() else 0
            await storage.require_capacity(
                session, {"symbols/public_cache": len(sym_bytes) - previous_size}
            )
            sym_row = Symbol(
                project_id=None,
                app_version=None,
                source=source,
                module_name=header.module_name,
                debug_file=header.debug_file,
                debug_id=header.debug_id,
                code_file=header.code_file,
                code_id=header.code_id,
                has_pe_unwind=has_pe_unwind,
                sym_path=rel_sym_path,
                sym_size=len(sym_bytes),
                raw_archive_path=None,
                raw_size=0,
                last_used_at=now,
                created_at=now,
            )
            try:
                async with session.begin_nested():
                    session.add(sym_row)
                    await session.flush()
            except IntegrityError:
                existing = (await session.execute(existing_stmt)).scalars().first()
                if existing is None:
                    raise
            else:
                await asyncio.to_thread(storage.atomic_write_bytes, rel_sym_path, sym_bytes)
                has_raw_inputs = staged_pdb is not None or staged_pe is not None
                if settings.ARCHIVE_RAW_SYMBOLS and has_raw_inputs:
                    raw_rel, raw_sz = await _archive_raw_files(
                        session,
                        storage,
                        header,
                        staged_pdb,
                        staged_pe,
                    )
                    sym_row.raw_archive_path = raw_rel
                    sym_row.raw_size = raw_sz
                _clear_msdl_negative_cache(storage, header.debug_file, header.debug_id)
                await session.commit()
                await session.refresh(sym_row)
                return SymbolIngestResult(symbol=sym_row, deduplicated=False, upgraded=False)

        assert existing is not None
        should_upgrade = has_pe_unwind and not existing.has_pe_unwind
        current_path = abs_sym_path
        if existing.sym_path and existing.sym_path != rel_sym_path:
            try:
                current_path = _resolve_in_partition(
                    storage, "symbols/public_cache", existing.sym_path
                )
            except SymbolConversionError:
                current_path = abs_sym_path

        if staged_pe is not None and not should_upgrade:
            try:
                with current_path.open(encoding="utf-8", errors="replace") as current:
                    stored_header = _parse_sym_records(current)
                should_upgrade = (
                    not stored_header.has_unwind_records
                    or stored_header.code_id != header.code_id
                )
            except (FileNotFoundError, SymbolConversionError):
                should_upgrade = True

        if should_upgrade:
            previous_size = abs_sym_path.stat().st_size if abs_sym_path.is_file() else 0
            await storage.require_capacity(
                session, {"symbols/public_cache": len(sym_bytes) - previous_size}
            )
            await asyncio.to_thread(storage.atomic_write_bytes, rel_sym_path, sym_bytes)
            _unlink_public_sym_if_distinct(storage, existing.sym_path, rel_sym_path)
            existing.module_name = header.module_name
            existing.debug_file = header.debug_file
            existing.code_file = header.code_file or existing.code_file
            existing.code_id = header.code_id or existing.code_id
            existing.has_pe_unwind = has_pe_unwind
            existing.sym_path = rel_sym_path
            existing.sym_size = len(sym_bytes)
            if source == "upload" or existing.source == "public_cache":
                existing.source = source
            if settings.ARCHIVE_RAW_SYMBOLS and (staged_pdb is not None or staged_pe is not None):
                raw_rel, raw_sz = await _archive_raw_files(
                    session, storage, header, staged_pdb, staged_pe
                )
                if raw_rel is not None:
                    if staged_pdb is None and existing.raw_archive_path:
                        if raw_rel != existing.raw_archive_path:
                            existing.raw_size = (existing.raw_size or 0) + raw_sz
                    else:
                        existing.raw_archive_path = raw_rel
                        existing.raw_size = raw_sz
            existing.last_used_at = now
            _clear_msdl_negative_cache(storage, header.debug_file, header.debug_id)
            await session.commit()
            await session.refresh(existing)
            return SymbolIngestResult(symbol=existing, deduplicated=True, upgraded=True)

        if not current_path.is_file():
            await storage.require_capacity(session, {"symbols/public_cache": len(sym_bytes)})
            await asyncio.to_thread(storage.atomic_write_bytes, rel_sym_path, sym_bytes)
            existing.sym_path = rel_sym_path
            existing.sym_size = len(sym_bytes)
        if source == "upload" and existing.source != "upload":
            existing.source = "upload"
        existing.last_used_at = now
        _clear_msdl_negative_cache(storage, header.debug_file, header.debug_id)
        await session.commit()
        await session.refresh(existing)
        return SymbolIngestResult(symbol=existing, deduplicated=True, upgraded=False)

    if acquire_capacity_lock:
        async with storage.shared_lock("capacity"):
            return await _persist()
    return await _persist()


async def ingest_public_symbol(
    session: AsyncSession,
    storage: StorageManager,
    settings: Settings,
    *,
    pdb_filename: str | None = None,
    pdb_bytes_or_path: bytes | Path | BinaryIO | None = None,
    pe_filename: str | None = None,
    pe_bytes_or_path: bytes | Path | BinaryIO | None = None,
    source: str = "upload",
    exec_fn: Any = None,
) -> SymbolIngestResult:
    """Ingest a project-less, version-less public symbol (.pdb + optional .dll/.exe, or .sym)."""
    if pdb_bytes_or_path is None and pe_bytes_or_path is None:
        raise SymbolConversionError("At least one of PDB, SYM, or PE file must be provided")

    is_direct_sym = bool(pdb_filename and pdb_filename.strip().lower().endswith(".sym"))
    if is_direct_sym:
        if pe_bytes_or_path is not None:
            raise SymbolConversionError(
                "Cannot combine a pre-converted .sym file with a PE companion binary"
            )
        async with storage.shared_lock("capacity"):
            incoming = _input_size(pdb_bytes_or_path)
            if incoming <= 0:
                raise SymbolConversionError("Uploaded .sym file is empty")
            if incoming > MAX_SYMBOL_OUTPUT_BYTES:
                raise SymbolConversionError(
                    "Uploaded .sym file exceeds the 128 MiB size limit"
                )
            if isinstance(pdb_bytes_or_path, bytes):
                sym_bytes = pdb_bytes_or_path
            elif isinstance(pdb_bytes_or_path, Path):
                sym_bytes = await asyncio.to_thread(pdb_bytes_or_path.read_bytes)
            else:
                assert pdb_bytes_or_path is not None
                pdb_bytes_or_path.seek(0)
                sym_bytes = await asyncio.to_thread(
                    pdb_bytes_or_path.read, MAX_SYMBOL_OUTPUT_BYTES + 1
                )
            if len(sym_bytes) > MAX_SYMBOL_OUTPUT_BYTES:
                raise SymbolConversionError(
                    "Uploaded .sym file exceeds the 128 MiB size limit"
                )
            header = parse_sym_header(sym_bytes.decode("utf-8", errors="replace"))
            return await store_public_symbol(
                session,
                storage,
                settings,
                sym_bytes=sym_bytes,
                header=header,
                source=source,
                acquire_capacity_lock=False,
            )

    async with storage.shared_lock("symbol-conversion"):
        with storage.staging_workspace("pubsym") as staging_dir:
            async with storage.shared_lock("capacity"):
                incoming = _input_size(pdb_bytes_or_path) + _input_size(pe_bytes_or_path)
                await storage.require_capacity(session, {"staging": incoming})
                staged_pdb = await asyncio.to_thread(
                    _stage_input, staging_dir, pdb_filename, pdb_bytes_or_path, "pdb"
                )
                if staged_pdb is not None and pe_filename == staged_pdb.name:
                    raise SymbolConversionError("PDB and PE must have distinct filenames")
                staged_pe = await asyncio.to_thread(
                    _stage_input, staging_dir, pe_filename, pe_bytes_or_path, "pe"
                )

            sym_bytes, pe_used = await run_dump_syms(
                pdb_path=staged_pdb,
                pe_path=staged_pe,
                settings=settings,
                exec_fn=exec_fn,
            )
            header = parse_sym_header(sym_bytes.decode("utf-8", errors="replace"))
            async with storage.shared_lock("capacity"):
                return await store_public_symbol(
                    session,
                    storage,
                    settings,
                    sym_bytes=sym_bytes,
                    header=header,
                    source=source,
                    staged_pdb=staged_pdb,
                    staged_pe=staged_pe if pe_used and header.has_unwind_records else None,
                    acquire_capacity_lock=False,
                )


def _scan_unindexed_public_syms(
    pub_root: Path,
    data_dir: Path,
    known_paths: set[str],
    known_keys: set[tuple[str, str]],
) -> list[tuple[str, int, ParsedSymHeader]]:
    found: list[tuple[str, int, ParsedSymHeader]] = []
    if not pub_root.exists():
        return found
    pub_resolved = pub_root.resolve()
    data_resolved = data_dir.resolve()
    try:
        for path in pub_root.rglob("*.sym"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(pub_resolved):
                    continue
                rel = resolved.relative_to(data_resolved).as_posix()
            except (OSError, ValueError):
                continue
            if "/_msdl_miss/" in f"/{rel}" or rel in known_paths:
                continue
            try:
                size = resolved.stat().st_size
                with resolved.open("r", encoding="utf-8", errors="replace") as fh:
                    header = _parse_sym_records(fh)
            except (OSError, SymbolConversionError):
                continue
            key = (header.debug_file.lower(), header.debug_id.upper())
            if key in known_keys:
                continue
            known_paths.add(rel)
            known_keys.add(key)
            found.append((rel, size, header))
    except OSError:
        return found
    return found


async def reconcile_public_symbol_cache(
    session: AsyncSession,
    storage: StorageManager,
) -> int:
    """Register any pre-existing .sym files on disk in symbols/public_cache into the DB."""
    pub_root = storage.partition_path("symbols/public_cache")
    if not pub_root.exists():
        return 0

    existing_rows = (
        await session.execute(
            select(Symbol.sym_path, Symbol.debug_file, Symbol.debug_id).where(
                Symbol.project_id.is_(None)
            )
        )
    ).all()
    known_paths = {row[0] for row in existing_rows if row[0]}
    known_keys = {(row[1].lower(), row[2].upper()) for row in existing_rows}

    discovered = await asyncio.to_thread(
        _scan_unindexed_public_syms,
        pub_root,
        storage.data_dir,
        known_paths,
        known_keys,
    )
    if not discovered:
        return 0

    now = utcnow()
    added = 0
    for rel_path, size, header in discovered:
        row = Symbol(
            project_id=None,
            app_version=None,
            source="public_cache",
            module_name=header.module_name,
            debug_file=header.debug_file,
            debug_id=header.debug_id,
            code_file=header.code_file,
            code_id=header.code_id,
            has_pe_unwind=header.has_unwind_records,
            sym_path=rel_path,
            sym_size=size,
            raw_archive_path=None,
            raw_size=0,
            last_used_at=now,
            created_at=now,
        )
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
            added += 1
        except IntegrityError:
            continue

    if added > 0:
        await session.commit()
    return added


async def resolve_symsrv_file(
    session: AsyncSession,
    storage: StorageManager,
    filename: str,
    symbol_id: str,
) -> tuple[Path, Symbol] | None:
    """Resolve a raw archived symbol for WinDbg /symsrv/{filename}/{symbol_id}/{filename}."""
    if (
        not filename
        or not symbol_id
        or filename in {".", ".."}
        or symbol_id in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
        or ".." in filename
        or "/" in symbol_id
        or "\\" in symbol_id
        or "\x00" in symbol_id
        or ".." in symbol_id
    ):
        return None

    fname_low = filename.lower()
    sid_low = symbol_id.lower()

    stmt = (
        select(Symbol)
        .where(
            or_(
                and_(
                    func.lower(Symbol.debug_file) == fname_low,
                    func.lower(Symbol.debug_id) == sid_low,
                ),
                and_(
                    Symbol.code_file.is_not(None),
                    Symbol.code_id.is_not(None),
                    func.lower(Symbol.code_file) == fname_low,
                    func.lower(Symbol.code_id) == sid_low,
                ),
            )
        )
        .order_by(Symbol.last_used_at.desc())
    )
    symbols = (await session.execute(stmt)).scalars().all()
    if not symbols:
        return None

    raw_root = storage.partition_path("symbols/raw")
    for sym in symbols:
        candidate_rels: list[str] = []
        for id_candidate in {symbol_id, symbol_id.upper(), sym.debug_id, sym.code_id}:
            if id_candidate:
                candidate_rels.append(f"symbols/raw/{id_candidate}/{filename}.zst")
        if sym.raw_archive_path:
            candidate_rels.append(sym.raw_archive_path)

        found_path: Path | None = None
        for rel in candidate_rels:
            try:
                p = storage.resolve_path(rel)
            except StorageError:
                continue
            if p.is_file() and p.name.lower() == f"{fname_low}.zst":
                found_path = p
                break

        # Fallback case-insensitive directory scan in symbols/raw
        if found_path is None and raw_root.exists():
            try:
                with os.scandir(raw_root) as dirs:
                    for dir_entry in dirs:
                        if not dir_entry.is_dir(follow_symlinks=False):
                            continue
                        if dir_entry.name.lower() != sid_low:
                            continue
                        with os.scandir(dir_entry.path) as files:
                            for file_entry in files:
                                if (
                                    file_entry.is_file(follow_symlinks=False)
                                    and file_entry.name.lower() == f"{fname_low}.zst"
                                ):
                                    found_path = Path(file_entry.path)
                                    break
                        if found_path is not None:
                            break
            except OSError:
                pass

        if found_path is not None:
            sym.last_used_at = utcnow()
            await session.commit()
            await session.refresh(sym)
            return found_path, sym

    return None


def stream_decompressed_zst(zst_path: Path, chunk_size: int = 65536) -> Iterator[bytes]:
    """Yield decompressed byte chunks from a .zst archive with bounded memory."""
    decompressor = zstd.ZstdDecompressor()
    with open(zst_path, "rb") as fh:
        with decompressor.stream_reader(fh) as reader:
            while True:
                chunk = reader.read(chunk_size)
                if not chunk:
                    break
                yield chunk


__all__ = [
    "ParsedSymHeader",
    "SymbolConversionError",
    "SymbolIngestResult",
    "_parse_sym_records",
    "ingest_public_symbol",
    "ingest_symbol",
    "parse_sym_header",
    "reconcile_public_symbol_cache",
    "resolve_symsrv_file",
    "run_dump_syms",
    "store_public_symbol",
    "stream_decompressed_zst",
]
