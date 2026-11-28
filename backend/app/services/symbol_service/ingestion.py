"""Project and public symbol ingestion, deduplication, upgrade, and cache reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.db.models import Symbol
from app.services.storage import StorageError, StorageManager
from app.services.symbol_service.archiving import (
    _archive_raw_files,
    _build_raw_archive_paths,
    _input_size,
    _stage_input,
)
from app.services.symbol_service.conversion import run_dump_syms
from app.services.symbol_service.models import (
    MAX_SYMBOL_OUTPUT_BYTES,
    ParsedSymHeader,
    SymbolConversionError,
    SymbolIngestResult,
)
from app.services.symbol_service.parsing import (
    _parse_sym_records,
    _resolve_in_partition,
    _validate_path_segment,
    parse_sym_header,
)


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
            incoming = _input_size(pdb_bytes_or_path) + _input_size(pe_bytes_or_path)
            await storage.reserve_staging_scratch(session, staging_dir, incoming)
            await session.commit()
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
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(storage.project_sym_lock(exclusive=False))
                if settings.ARCHIVE_RAW_SYMBOLS:
                    await stack.enter_async_context(storage.raw_symbol_lock(exclusive=False))
                await stack.enter_async_context(storage.storage_io_lock(exclusive=False))
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

    # Follow global DB lock order: lock storage_partition_usage BEFORE symbols rows
    # so concurrent delete_symbol and _store_symbol upgrades can never deadlock on PostgreSQL.
    await storage.ensure_usage_ledger(session, lock=True)

    existing_stmt = select(Symbol).where(
        and_(
            Symbol.project_id == project_id,
            Symbol.debug_file == header.debug_file,
            Symbol.debug_id == header.debug_id,
        )
    )
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        existing_stmt = existing_stmt.with_for_update()
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()
    now = utcnow()

    if existing is None:
        previous_size = abs_sym_path.stat().st_size if abs_sym_path.is_file() else 0
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
                await storage.commit_usage_deltas(
                    session,
                    {"symbols/sym": len(sym_bytes) - previous_size},
                    {"symbols/sym": 0 if previous_size else 1},
                    enforce_quota=True,
                )
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
        await storage.commit_usage_deltas(
            session,
            {"symbols/sym": len(sym_bytes) - previous_size},
            {"symbols/sym": 0 if previous_size else 1},
            enforce_quota=True,
        )
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
        await storage.commit_usage_deltas(
            session,
            {"symbols/sym": len(sym_bytes)},
            {"symbols/sym": 1},
            enforce_quota=True,
        )
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
) -> int:
    if not old_rel or old_rel == new_rel:
        return 0
    try:
        old_abs = _resolve_in_partition(storage, "symbols/public_cache", old_rel)
        return storage._safe_unlink(old_abs)
    except (SymbolConversionError, OSError):
        return 0


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
    _ = acquire_capacity_lock

    async def _persist() -> SymbolIngestResult:
        has_pe_unwind = header.has_unwind_records
        rel_sym_path = (
            f"symbols/public_cache/{header.debug_file}/{header.debug_id}/{header.stem}.sym"
        )
        abs_sym_path = _resolve_in_partition(storage, "symbols/public_cache", rel_sym_path)
        _build_raw_archive_paths(storage, header, staged_pdb, staged_pe)

        # Follow global DB lock order: storage_partition_usage BEFORE symbols rows.
        await storage.ensure_usage_ledger(session, lock=True)

        existing_stmt = select(Symbol).where(
            and_(
                Symbol.project_id.is_(None),
                func.lower(Symbol.debug_file) == header.debug_file.lower(),
                func.upper(Symbol.debug_id) == header.debug_id.upper(),
            )
        )
        bind = session.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            existing_stmt = existing_stmt.with_for_update()
        existing = (await session.execute(existing_stmt)).scalars().first()
        now = utcnow()

        if existing is None:
            previous_size = abs_sym_path.stat().st_size if abs_sym_path.is_file() else 0
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
                    await storage.commit_usage_deltas(
                        session,
                        {"symbols/public_cache": len(sym_bytes) - previous_size},
                        {"symbols/public_cache": 0 if previous_size else 1},
                        enforce_quota=True,
                    )
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
            old_distinct_size = (
                current_path.stat().st_size
                if existing.sym_path != rel_sym_path and current_path.is_file()
                else 0
            )
            file_delta = (0 if previous_size else 1) - (1 if old_distinct_size else 0)
            await storage.commit_usage_deltas(
                session,
                {"symbols/public_cache": len(sym_bytes) - previous_size - old_distinct_size},
                {"symbols/public_cache": file_delta},
                enforce_quota=True,
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
            await storage.commit_usage_deltas(
                session,
                {"symbols/public_cache": len(sym_bytes)},
                {"symbols/public_cache": 1},
                enforce_quota=True,
            )
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

    # Striped per-symbol lock serializes concurrent lookup/measurement/upgrade of the
    # same (debug_file, debug_id) while keeping distinct public symbols parallel.
    symbol_digest = hashlib.sha256(
        f"{header.debug_file.lower()}:{header.debug_id.upper()}".encode()
    ).hexdigest()
    stripe = int(symbol_digest[:8], 16) % 32
    needs_raw_lock = bool(
        settings.ARCHIVE_RAW_SYMBOLS and (staged_pdb is not None or staged_pe is not None)
    )

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(storage.shared_lock(f"pubsym-{stripe:02d}"))
        await stack.enter_async_context(storage.public_cache_lock(exclusive=False))
        if needs_raw_lock:
            await stack.enter_async_context(storage.raw_symbol_lock(exclusive=False))
        await stack.enter_async_context(storage.storage_io_lock(exclusive=False))
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
        incoming = _input_size(pdb_bytes_or_path)
        if incoming <= 0:
            raise SymbolConversionError("Uploaded .sym file is empty")
        if incoming > MAX_SYMBOL_OUTPUT_BYTES:
            raise SymbolConversionError("Uploaded .sym file exceeds the 128 MiB size limit")
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
            raise SymbolConversionError("Uploaded .sym file exceeds the 128 MiB size limit")
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
            incoming = _input_size(pdb_bytes_or_path) + _input_size(pe_bytes_or_path)
            await storage.reserve_staging_scratch(session, staging_dir, incoming)
            await session.commit()
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

    # Fast read-only pre-check to avoid taking exclusive locks when disk and DB already match.
    pre_rows = (
        await session.execute(
            select(Symbol.sym_path, Symbol.debug_file, Symbol.debug_id).where(
                Symbol.project_id.is_(None)
            )
        )
    ).all()
    pre_discovered = await asyncio.to_thread(
        _scan_unindexed_public_syms,
        pub_root,
        storage.data_dir,
        {row[0] for row in pre_rows if row[0]},
        {(row[1].lower(), row[2].upper()) for row in pre_rows},
    )
    if not pre_discovered:
        return 0

    # Follow global lock order (public_cache_lock -> storage_io_lock -> DB inserts)
    # so reconciliation can never deadlock with concurrent store_public_symbol uploads.
    async with storage.public_cache_lock(exclusive=False):
        async with storage.storage_io_lock(exclusive=True):
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
                await storage.reconcile_usage(session)
                await session.commit()
            return added
