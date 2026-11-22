"""Breakpad symbol conversion (dump_syms), deduplication, raw archiving, and SymSrv."""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import zstandard as zstd
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.db.models import Symbol
from app.services.storage import StorageError, StorageManager


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
    os_name = ""
    arch = ""
    debug_id = ""
    debug_file = ""
    code_id: str | None = None
    code_file: str | None = None
    has_unwind_records = False
    found_module = False

    for raw_line in io.StringIO(sym_text):
        line = raw_line.rstrip("\r\n")
        upper_line = line.upper()
        if not found_module and upper_line.startswith("MODULE "):
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
            if found_module and code_id is not None:
                break

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
    exec_callable = exec_fn or asyncio.create_subprocess_exec
    try:
        proc = await exec_callable(
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
    except TimeoutError as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()
        raise SymbolConversionError(
            f"dump_syms timed out after {timeout_seconds}s: {' '.join(cmd)}"
        ) from exc

    rc = proc.returncode if proc.returncode is not None else -1
    return rc, stdout or b"", stderr or b""


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

    if pe_path is not None and pdb_path is not None:
        rc, stdout, stderr = await _run_subprocess(
            [bin_path, str(pe_path), str(pdb_path)],
            timeout_seconds=timeout,
            exec_fn=exec_fn,
        )
        if rc == 0 and b"MODULE " in stdout:
            return stdout, True

        # Fallback to PDB-only invocation
        rc_fb, stdout_fb, stderr_fb = await _run_subprocess(
            [bin_path, str(pdb_path)],
            timeout_seconds=timeout,
            exec_fn=exec_fn,
        )
        if rc_fb == 0 and b"MODULE " in stdout_fb:
            return stdout_fb, False

        err_msg = (stderr_fb or stderr).decode("utf-8", errors="replace").strip()
        raise SymbolConversionError(
            f"dump_syms failed for PE+PDB and PDB fallback: {err_msg or f'exit {rc_fb}'}"
        )

    target = pe_path if pe_path is not None else pdb_path
    assert target is not None
    rc, stdout, stderr = await _run_subprocess(
        [bin_path, str(target)],
        timeout_seconds=timeout,
        exec_fn=exec_fn,
    )
    if rc == 0 and b"MODULE " in stdout:
        return stdout, pe_path is not None

    err_msg = stderr.decode("utf-8", errors="replace").strip()
    raise SymbolConversionError(f"dump_syms failed: {err_msg or f'exit {rc}'}")


def _stage_input(
    staging_dir: Path,
    filename: str | None,
    data_or_path: bytes | Path | None,
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
    dest.write_bytes(data_or_path)
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


def _archive_raw_files(
    storage: StorageManager,
    header: ParsedSymHeader,
    pdb_path: Path | None,
    pe_path: Path | None,
) -> tuple[str | None, int]:
    """Compress raw PDB/PE files into symbols/raw if safety floor permits."""
    rel_pdb, rel_pe = _build_raw_archive_paths(storage, header, pdb_path, pe_path)
    if storage.is_host_floor_breached():
        return None, 0

    primary_rel: str | None = None
    total_raw_size = 0

    if pdb_path is not None and rel_pdb is not None:
        try:
            archived = storage.compress_raw_symbol(pdb_path, rel_pdb)
        except StorageError as exc:
            raise SymbolConversionError(f"Failed to archive raw PDB: {exc}") from exc
        if archived is not None:
            primary_rel, sz = archived
            total_raw_size += sz

    if pe_path is not None and rel_pe is not None:
        try:
            archived = storage.compress_raw_symbol(pe_path, rel_pe)
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
    pdb_bytes_or_path: bytes | Path | None = None,
    pe_filename: str | None = None,
    pe_bytes_or_path: bytes | Path | None = None,
    exec_fn: Any = None,
) -> SymbolIngestResult:
    """Convert PDB/PE via dump_syms, deduplicate/upgrade in DB, and archive raw symbols."""
    if pdb_bytes_or_path is None and pe_bytes_or_path is None:
        raise SymbolConversionError("At least one of PDB or PE file must be provided")

    staging_dir = storage.partition_path("staging") / f"sym_{uuid.uuid4().hex}"
    staging_dir.mkdir(parents=True, exist_ok=True)
    try:
        staged_pdb = _stage_input(staging_dir, pdb_filename, pdb_bytes_or_path, "pdb")
        staged_pe = _stage_input(staging_dir, pe_filename, pe_bytes_or_path, "pe")

        sym_bytes, pe_used = await run_dump_syms(
            pdb_path=staged_pdb,
            pe_path=staged_pe,
            settings=settings,
            exec_fn=exec_fn,
        )
        sym_text = sym_bytes.decode("utf-8", errors="replace")
        header = parse_sym_header(sym_text)
        has_pe_unwind = bool((staged_pe is not None and pe_used) or header.has_unwind_records)

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
                storage.atomic_write_bytes(rel_sym_path, sym_bytes)
                if settings.ARCHIVE_RAW_SYMBOLS:
                    raw_rel, raw_sz = _archive_raw_files(
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
        should_upgrade = (not existing.has_pe_unwind) and has_pe_unwind
        if should_upgrade:
            storage.atomic_write_bytes(rel_sym_path, sym_bytes)
            existing.app_version = app_version
            existing.module_name = header.module_name
            existing.code_file = header.code_file or existing.code_file
            existing.code_id = header.code_id or existing.code_id
            existing.has_pe_unwind = True
            existing.sym_path = rel_sym_path
            existing.sym_size = len(sym_bytes)
            if settings.ARCHIVE_RAW_SYMBOLS:
                raw_rel, raw_sz = _archive_raw_files(storage, header, staged_pdb, staged_pe)
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
            storage.atomic_write_bytes(rel_sym_path, sym_bytes)
            existing.sym_size = len(sym_bytes)
        existing.last_used_at = now
        await session.commit()
        await session.refresh(existing)
        return SymbolIngestResult(symbol=existing, deduplicated=True, upgraded=False)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


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
    "ingest_symbol",
    "parse_sym_header",
    "resolve_symsrv_file",
    "run_dump_syms",
    "stream_decompressed_zst",
]
