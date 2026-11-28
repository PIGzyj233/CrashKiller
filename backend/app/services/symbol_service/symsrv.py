"""WinDbg SymSrv archived file lookup and streaming zstd decompression."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

import zstandard as zstd
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import Symbol
from app.services.storage import StorageError, StorageManager


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


def stream_decompressed_zst_handle(fh: BinaryIO, chunk_size: int = 65536) -> Iterator[bytes]:
    """Yield decompressed byte chunks from an already-open .zst file handle and close it."""
    decompressor = zstd.ZstdDecompressor()
    try:
        with decompressor.stream_reader(fh) as reader:
            while True:
                chunk = reader.read(chunk_size)
                if not chunk:
                    break
                yield chunk
    finally:
        fh.close()


def stream_decompressed_zst(zst_path: Path, chunk_size: int = 65536) -> Iterator[bytes]:
    """Yield decompressed byte chunks from a .zst archive with bounded memory."""
    fh = open(zst_path, "rb")
    yield from stream_decompressed_zst_handle(fh, chunk_size=chunk_size)
