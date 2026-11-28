"""Input staging and compressed raw PDB/PE archiving helpers."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import BinaryIO

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.storage import StorageCapacityError, StorageError, StorageManager
from app.services.symbol_service.models import ParsedSymHeader, SymbolConversionError
from app.services.symbol_service.parsing import (
    _resolve_in_partition,
    _sanitize_filename,
    _validate_path_segment,
    clamp_upload_filename,
)


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
    raw_byte_delta = 0
    raw_file_delta = 0

    if pdb_path is not None and rel_pdb is not None:
        pdb_target = storage.resolve_path(rel_pdb)
        old_pdb_size = pdb_target.stat().st_size if pdb_target.is_file() else 0
        try:
            archived = await asyncio.to_thread(storage.compress_raw_symbol, pdb_path, rel_pdb)
        except StorageError as exc:
            raise SymbolConversionError(f"Failed to archive raw PDB: {exc}") from exc
        if archived is not None:
            primary_rel, sz = archived
            total_raw_size += sz
            raw_byte_delta += sz - old_pdb_size
            if old_pdb_size == 0:
                raw_file_delta += 1

    if pe_path is not None and rel_pe is not None:
        pe_target = storage.resolve_path(rel_pe)
        old_pe_size = pe_target.stat().st_size if pe_target.is_file() else 0
        try:
            archived = await asyncio.to_thread(storage.compress_raw_symbol, pe_path, rel_pe)
        except StorageError as exc:
            raise SymbolConversionError(f"Failed to archive raw PE: {exc}") from exc
        if archived is not None:
            rel_pe_out, sz = archived
            if primary_rel is None:
                primary_rel = rel_pe_out
            total_raw_size += sz
            raw_byte_delta += sz - old_pe_size
            if old_pe_size == 0:
                raw_file_delta += 1

    if raw_byte_delta != 0 or raw_file_delta != 0:
        await storage.commit_usage_deltas(
            session,
            {"symbols/raw": raw_byte_delta},
            {"symbols/raw": raw_file_delta},
            enforce_quota=False,
        )

    return primary_rel, total_raw_size
