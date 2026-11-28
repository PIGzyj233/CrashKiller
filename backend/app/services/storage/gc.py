"""TTL and quota/emergency LRU garbage collection with batched row-level claims."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CrashJob, CrashLog, CrashReport, Issue, Project, Symbol
from app.services.storage.constants import ACTIVE_CRASH_STATUSES, GIB, PARTITIONS, StorageError
from app.services.storage.gc_staging import (
    collect_evictable_staging_candidates,
    load_active_queued_symbol_upload_ids,
    load_active_upload_crash_ids,
    unlink_evictable_staging_candidates,
)

if TYPE_CHECKING:
    from app.services.storage.manager import StorageManager

GC_BATCH_SIZE = 200


@dataclass
class _GCStats:
    freed_bytes: int = 0
    deleted_files_count: int = 0
    skipped_pinned_dumps: set[uuid.UUID] = field(default_factory=set)
    skipped_pinned_logs: set[uuid.UUID] = field(default_factory=set)


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _needs_emergency_reclaim(
    storage: StorageManager,
    *,
    force_emergency: bool,
    emergency: bool,
) -> bool:
    return force_emergency or (emergency and storage.is_host_floor_breached())


def _crash_has_active_job() -> Any:
    return select(CrashJob.id).where(CrashJob.crash_id == CrashReport.id).exists()


def _crash_issue_is_pinned() -> Any:
    return (
        select(Issue.id)
        .where(Issue.id == CrashReport.issue_id, Issue.is_pinned.is_(True))
        .exists()
    )


async def _claim_dump_eviction(
    session: AsyncSession,
    report_id: uuid.UUID,
    expected_dump_path: str,
) -> bool:
    """Atomically clear CrashReport.dump_path only if idle, unpinned, and job-free."""
    claimed = await session.scalar(
        update(CrashReport)
        .where(
            CrashReport.id == report_id,
            CrashReport.dump_path == expected_dump_path,
            CrashReport.is_pinned.is_(False),
            CrashReport.status.notin_(ACTIVE_CRASH_STATUSES),
            ~_crash_has_active_job(),
            ~_crash_issue_is_pinned(),
        )
        .values(dump_path=None)
        .returning(CrashReport.id)
    )
    return claimed is not None


async def _claim_log_eviction(
    session: AsyncSession,
    log_id: uuid.UUID,
    crash_id: uuid.UUID,
    expected_zst_path: str,
) -> bool:
    """Lock the parent CrashReport row first, then atomically clear CrashLog paths."""
    bind = session.get_bind()
    is_pg = bind is not None and bind.dialect.name == "postgresql"
    parent_stmt = select(CrashReport).where(CrashReport.id == crash_id)
    if is_pg:
        parent_stmt = parent_stmt.with_for_update()
    parent = (await session.execute(parent_stmt)).scalar_one_or_none()
    if parent is None or parent.is_pinned or parent.status in ACTIVE_CRASH_STATUSES:
        return False
    has_job = await session.scalar(
        select(CrashJob.id).where(CrashJob.crash_id == crash_id).limit(1)
    )
    if has_job is not None:
        return False
    if parent.issue_id is not None:
        issue_pinned = await session.scalar(
            select(Issue.is_pinned).where(Issue.id == parent.issue_id)
        )
        if issue_pinned:
            return False

    claimed = await session.scalar(
        update(CrashLog)
        .where(
            CrashLog.id == log_id,
            CrashLog.zst_path == expected_zst_path,
        )
        .values(zst_path=None, idx_path=None)
        .returning(CrashLog.id)
    )
    return claimed is not None


def _unlink_dump_path(
    storage: StorageManager,
    dumps_root: Path,
    rel_dump_path: str,
) -> int:
    try:
        target = storage.resolve_path(rel_dump_path)
        removed = storage._safe_unlink(target)
        storage._prune_empty_dirs(target.parent, dumps_root)
        return removed
    except StorageError:
        return 0


def _unlink_log_paths(
    storage: StorageManager,
    logs_root: Path,
    zst_path: str | None,
    idx_path: str | None,
) -> tuple[int, int]:
    freed = 0
    deleted = 0
    for rel_attr in (zst_path, idx_path):
        if not rel_attr:
            continue
        try:
            target = storage.resolve_path(rel_attr)
            removed = storage._safe_unlink(target)
            if removed > 0:
                freed += removed
                deleted += 1
            storage._prune_empty_dirs(target.parent, logs_root)
        except StorageError:
            pass
    return freed, deleted


async def _evict_expired_dumps_and_logs(
    storage: StorageManager,
    session: AsyncSession,
    stats: _GCStats,
    *,
    now: datetime,
) -> tuple[Path, Path]:
    from app.services.settings_service import (
        default_effective_settings,
        resolve_project_settings,
    )

    effective_baseline = default_effective_settings(storage.settings)
    projects = (await session.execute(select(Project))).scalars().all()
    retention_by_project: dict[uuid.UUID, tuple[datetime, datetime]] = {}
    for proj in projects:
        resolved = resolve_project_settings(effective_baseline, proj.settings_json)
        retention_by_project[proj.id] = (
            now - timedelta(days=int(resolved["dump_retention_days"])),
            now - timedelta(days=int(resolved["log_retention_days"])),
        )
    default_dump_cutoff = now - timedelta(days=storage.settings.DUMP_RETENTION_DAYS)
    default_log_cutoff = now - timedelta(days=storage.settings.LOG_RETENTION_DAYS)
    max_dump_cutoff = max(
        [default_dump_cutoff, *(pair[0] for pair in retention_by_project.values())]
    )
    max_log_cutoff = max(
        [default_log_cutoff, *(pair[1] for pair in retention_by_project.values())]
    )

    dumps_root = storage.partition_path("dumps")
    dump_cursor: tuple[datetime, uuid.UUID] | None = None
    while True:
        stmt = (
            select(
                CrashReport.id,
                CrashReport.project_id,
                CrashReport.created_at,
                CrashReport.dump_path,
                CrashReport.is_pinned,
                Issue.is_pinned.label("issue_pinned"),
            )
            .outerjoin(Issue, CrashReport.issue_id == Issue.id)
            .where(
                CrashReport.dump_path.is_not(None),
                CrashReport.status.notin_(ACTIVE_CRASH_STATUSES),
                CrashReport.created_at < max_dump_cutoff,
            )
            .order_by(CrashReport.created_at.asc(), CrashReport.id.asc())
            .limit(GC_BATCH_SIZE)
        )
        if dump_cursor is not None:
            c_ts, c_id = dump_cursor
            stmt = stmt.where(
                or_(
                    CrashReport.created_at > c_ts,
                    and_(CrashReport.created_at == c_ts, CrashReport.id > c_id),
                )
            )

        rows = (await session.execute(stmt)).all()
        if not rows:
            break
        dump_cursor = (rows[-1][2], rows[-1][0])

        claimed_paths: list[str] = []
        for report_id, project_id, created_at, dump_path, is_pinned, issue_pinned in rows:
            dump_cutoff = retention_by_project.get(
                project_id,
                (default_dump_cutoff, default_log_cutoff),
            )[0]
            if _as_utc(created_at) >= dump_cutoff:
                continue
            if is_pinned or bool(issue_pinned):
                stats.skipped_pinned_dumps.add(report_id)
                continue
            if dump_path and await _claim_dump_eviction(session, report_id, dump_path):
                claimed_paths.append(dump_path)

        if claimed_paths:
            await session.commit()
            for dump_path in claimed_paths:
                removed = _unlink_dump_path(storage, dumps_root, dump_path)
                if removed > 0:
                    stats.freed_bytes += removed
                    stats.deleted_files_count += 1

        if len(rows) < GC_BATCH_SIZE:
            break

    logs_root = storage.partition_path("logs")
    log_cursor: tuple[datetime, uuid.UUID] | None = None
    while True:
        stmt = (
            select(
                CrashLog.id,
                CrashLog.crash_id,
                CrashLog.zst_path,
                CrashLog.idx_path,
                CrashReport.project_id,
                CrashReport.created_at,
                CrashReport.is_pinned,
                Issue.is_pinned.label("issue_pinned"),
            )
            .join(CrashReport, CrashLog.crash_id == CrashReport.id)
            .outerjoin(Issue, CrashReport.issue_id == Issue.id)
            .where(
                CrashLog.zst_path.is_not(None),
                CrashReport.status.notin_(ACTIVE_CRASH_STATUSES),
                CrashReport.created_at < max_log_cutoff,
            )
            .order_by(CrashReport.created_at.asc(), CrashLog.id.asc())
            .limit(GC_BATCH_SIZE)
        )
        if log_cursor is not None:
            l_ts, l_id = log_cursor
            stmt = stmt.where(
                or_(
                    CrashReport.created_at > l_ts,
                    and_(CrashReport.created_at == l_ts, CrashLog.id > l_id),
                )
            )

        rows = (await session.execute(stmt)).all()
        if not rows:
            break
        log_cursor = (rows[-1][5], rows[-1][0])

        claimed_logs: list[tuple[str | None, str | None]] = []
        for (
            log_id,
            crash_id,
            zst_path,
            idx_path,
            project_id,
            created_at,
            is_pinned,
            issue_pinned,
        ) in rows:
            log_cutoff = retention_by_project.get(
                project_id,
                (default_dump_cutoff, default_log_cutoff),
            )[1]
            if _as_utc(created_at) >= log_cutoff:
                continue
            if is_pinned or bool(issue_pinned):
                stats.skipped_pinned_logs.add(log_id)
                continue
            if zst_path and await _claim_log_eviction(session, log_id, crash_id, zst_path):
                claimed_logs.append((zst_path, idx_path))

        if claimed_logs:
            await session.commit()
            for zst_path, idx_path in claimed_logs:
                freed, deleted = _unlink_log_paths(storage, logs_root, zst_path, idx_path)
                stats.freed_bytes += freed
                stats.deleted_files_count += deleted

        if len(rows) < GC_BATCH_SIZE:
            break

    return dumps_root, logs_root


async def _evict_public_cache(
    storage: StorageManager,
    session: AsyncSession,
    stats: _GCStats,
    *,
    threshold_ratio: float,
    force_emergency: bool,
    emergency: bool,
) -> None:
    pub_root = storage.partition_path("symbols/public_cache")
    pub_used, _ = await asyncio.to_thread(storage.partition_used_bytes, "symbols/public_cache")
    pub_target = int(storage.get_partition_quota_bytes("symbols/public_cache") * threshold_ratio)
    if pub_used <= pub_target and not _needs_emergency_reclaim(
        storage, force_emergency=force_emergency, emergency=emergency
    ):
        return

    async with storage.public_cache_lock(exclusive=True):
        pub_used, _ = await asyncio.to_thread(storage.partition_used_bytes, "symbols/public_cache")
        if pub_used <= pub_target and not _needs_emergency_reclaim(
            storage, force_emergency=force_emergency, emergency=emergency
        ):
            return

        pub_syms_stmt = select(Symbol).where(Symbol.project_id.is_(None))
        pub_syms = (await session.execute(pub_syms_stmt)).scalars().all()
        pub_by_path: dict[str, Symbol] = {s.sym_path: s for s in pub_syms if s.sym_path}
        data_root = storage.data_dir.resolve()
        aged_files = await asyncio.to_thread(storage._iter_files_sorted_by_age, pub_root)

        eviction_entries: list[tuple[int, float, Path, int, Symbol | None]] = []
        for mtime, path, size in aged_files:
            if path.name == "_msdl_circuit.json":
                continue
            try:
                rel_path = path.resolve().relative_to(data_root).as_posix()
            except (OSError, ValueError):
                rel_path = ""
            sym_row = pub_by_path.get(rel_path)
            if "/_msdl_miss/" in f"/{rel_path}":
                tier = 0
                sort_ts = mtime
            elif sym_row is None:
                tier = 1
                sort_ts = mtime
            elif sym_row.source != "upload":
                tier = 2
                sort_ts = _as_utc(sym_row.last_used_at).timestamp()
            else:
                tier = 3
                sort_ts = _as_utc(sym_row.last_used_at).timestamp()
            eviction_entries.append((tier, sort_ts, path, size, sym_row))

        eviction_entries.sort(key=lambda item: (item[0], item[1]))
        for _, _, path, size, sym_row in eviction_entries:
            if pub_used <= pub_target and not _needs_emergency_reclaim(
                storage, force_emergency=force_emergency, emergency=emergency
            ):
                break
            if sym_row is not None:
                await session.delete(sym_row)
                await session.commit()
            removed = storage._safe_unlink(path)
            if removed > 0:
                stats.freed_bytes += removed
                stats.deleted_files_count += 1
                pub_used = max(0, pub_used - size)
                storage._prune_empty_dirs(path.parent, pub_root)


async def _evict_raw_symbols(
    storage: StorageManager,
    session: AsyncSession,
    stats: _GCStats,
    *,
    threshold_ratio: float,
    force_emergency: bool,
    emergency: bool,
) -> Path:
    raw_root = storage.partition_path("symbols/raw")
    raw_used, _ = await asyncio.to_thread(storage.partition_used_bytes, "symbols/raw")
    raw_target = int(storage.get_partition_quota_bytes("symbols/raw") * threshold_ratio)
    if raw_used <= raw_target and not _needs_emergency_reclaim(
        storage, force_emergency=force_emergency, emergency=emergency
    ):
        return raw_root

    async with storage.raw_symbol_lock(exclusive=True):
        raw_used, _ = await asyncio.to_thread(storage.partition_used_bytes, "symbols/raw")
        if raw_used <= raw_target and not _needs_emergency_reclaim(
            storage, force_emergency=force_emergency, emergency=emergency
        ):
            return raw_root

        raw_syms_stmt = (
            select(Symbol)
            .where(Symbol.raw_archive_path.is_not(None))
            .order_by(Symbol.last_used_at.asc())
        )
        raw_syms = (await session.execute(raw_syms_stmt)).scalars().all()
        raw_counts: dict[str, int] = {}
        pe_counts: dict[tuple[str, str], int] = {}
        for s in raw_syms:
            if s.raw_archive_path:
                raw_counts[s.raw_archive_path] = raw_counts.get(s.raw_archive_path, 0) + 1
            if s.code_id and s.code_file and s.has_pe_unwind:
                pe_key = (s.code_id, s.code_file)
                pe_counts[pe_key] = pe_counts.get(pe_key, 0) + 1

        for sym in raw_syms:
            if raw_used <= raw_target and not _needs_emergency_reclaim(
                storage, force_emergency=force_emergency, emergency=emergency
            ):
                break
            orig_raw_rel = sym.raw_archive_path
            pe_raw_rel: str | None = None
            if sym.code_id and sym.code_file and sym.has_pe_unwind:
                pe_key = (sym.code_id, sym.code_file)
                pe_counts[pe_key] = pe_counts.get(pe_key, 1) - 1
                pe_raw_rel = f"symbols/raw/{sym.code_id}/{sym.code_file}.zst"

            unlink_targets: list[str] = []
            if orig_raw_rel:
                raw_counts[orig_raw_rel] = raw_counts.get(orig_raw_rel, 1) - 1
                pe_still_referenced = (
                    pe_raw_rel is not None
                    and orig_raw_rel == pe_raw_rel
                    and sym.code_id is not None
                    and sym.code_file is not None
                    and pe_counts.get((sym.code_id, sym.code_file), 0) > 0
                )
                if raw_counts[orig_raw_rel] <= 0 and not pe_still_referenced:
                    unlink_targets.append(orig_raw_rel)

            if (
                pe_raw_rel is not None
                and pe_raw_rel != orig_raw_rel
                and sym.code_id is not None
                and sym.code_file is not None
                and pe_counts.get((sym.code_id, sym.code_file), 0) <= 0
                and raw_counts.get(pe_raw_rel, 0) <= 0
            ):
                unlink_targets.append(pe_raw_rel)

            sym.raw_archive_path = None
            sym.raw_size = 0
            await session.commit()

            for rel_target in unlink_targets:
                try:
                    target = storage.resolve_path(rel_target)
                    removed = storage._safe_unlink(target)
                    if removed > 0:
                        stats.freed_bytes += removed
                        stats.deleted_files_count += 1
                        raw_used = max(0, raw_used - removed)
                    storage._prune_empty_dirs(target.parent, raw_root)
                except StorageError:
                    pass

        if raw_used > raw_target or _needs_emergency_reclaim(
            storage, force_emergency=force_emergency, emergency=emergency
        ):
            for _, path, size in await asyncio.to_thread(
                storage._iter_files_sorted_by_age, raw_root
            ):
                if raw_used <= raw_target and not _needs_emergency_reclaim(
                    storage, force_emergency=force_emergency, emergency=emergency
                ):
                    break
                removed = storage._safe_unlink(path)
                if removed > 0:
                    stats.freed_bytes += removed
                    stats.deleted_files_count += 1
                    raw_used = max(0, raw_used - size)
                    storage._prune_empty_dirs(path.parent, raw_root)

    return raw_root


async def _evict_dumps_lru(
    storage: StorageManager,
    session: AsyncSession,
    stats: _GCStats,
    *,
    dumps_root: Path,
    threshold_ratio: float,
    total_target: int,
    force_emergency: bool,
    emergency: bool,
) -> None:
    dumps_used, _ = await asyncio.to_thread(storage.partition_used_bytes, "dumps")
    dumps_target = int(storage.get_partition_quota_bytes("dumps") * threshold_ratio)
    fs_used = await asyncio.to_thread(
        lambda: sum(storage.partition_used_bytes(p)[0] for p in PARTITIONS)
    )
    if (
        not _needs_emergency_reclaim(storage, force_emergency=force_emergency, emergency=emergency)
        and dumps_used <= dumps_target
        and fs_used <= total_target
    ):
        return

    cursor: tuple[datetime, uuid.UUID] | None = None
    while True:
        if (
            dumps_used <= dumps_target
            and fs_used <= total_target
            and not _needs_emergency_reclaim(
                storage, force_emergency=force_emergency, emergency=emergency
            )
        ):
            break

        stmt = (
            select(
                CrashReport.id,
                CrashReport.created_at,
                CrashReport.dump_path,
                CrashReport.is_pinned,
                Issue.is_pinned.label("issue_pinned"),
            )
            .outerjoin(Issue, CrashReport.issue_id == Issue.id)
            .where(
                CrashReport.dump_path.is_not(None),
                CrashReport.status.notin_(ACTIVE_CRASH_STATUSES),
            )
            .order_by(CrashReport.created_at.asc(), CrashReport.id.asc())
            .limit(GC_BATCH_SIZE)
        )
        if cursor is not None:
            c_ts, c_id = cursor
            stmt = stmt.where(
                or_(
                    CrashReport.created_at > c_ts,
                    and_(CrashReport.created_at == c_ts, CrashReport.id > c_id),
                )
            )
        rows = (await session.execute(stmt)).all()
        if not rows:
            break
        cursor = (rows[-1][1], rows[-1][0])

        for report_id, _created_at, dump_path, is_pinned, issue_pinned in rows:
            if (
                dumps_used <= dumps_target
                and fs_used <= total_target
                and not _needs_emergency_reclaim(
                    storage, force_emergency=force_emergency, emergency=emergency
                )
            ):
                return
            if is_pinned or bool(issue_pinned):
                stats.skipped_pinned_dumps.add(report_id)
                continue
            if dump_path and await _claim_dump_eviction(session, report_id, dump_path):
                await session.commit()
                removed = _unlink_dump_path(storage, dumps_root, dump_path)
                if removed > 0:
                    stats.freed_bytes += removed
                    stats.deleted_files_count += 1
                    dumps_used = max(0, dumps_used - removed)
                    fs_used = max(0, fs_used - removed)

        if len(rows) < GC_BATCH_SIZE:
            break


async def _evict_logs_lru(
    storage: StorageManager,
    session: AsyncSession,
    stats: _GCStats,
    *,
    logs_root: Path,
    threshold_ratio: float,
    total_target: int,
    force_emergency: bool,
    emergency: bool,
) -> None:
    logs_used, _ = await asyncio.to_thread(storage.partition_used_bytes, "logs")
    logs_target = int(storage.get_partition_quota_bytes("logs") * threshold_ratio)
    fs_used = await asyncio.to_thread(
        lambda: sum(storage.partition_used_bytes(p)[0] for p in PARTITIONS)
    )
    if (
        not _needs_emergency_reclaim(storage, force_emergency=force_emergency, emergency=emergency)
        and logs_used <= logs_target
        and fs_used <= total_target
    ):
        return

    cursor: tuple[datetime, uuid.UUID] | None = None
    while True:
        if (
            logs_used <= logs_target
            and fs_used <= total_target
            and not _needs_emergency_reclaim(
                storage, force_emergency=force_emergency, emergency=emergency
            )
        ):
            break

        stmt = (
            select(
                CrashLog.id,
                CrashLog.crash_id,
                CrashLog.zst_path,
                CrashLog.idx_path,
                CrashReport.created_at,
                CrashReport.is_pinned,
                Issue.is_pinned.label("issue_pinned"),
            )
            .join(CrashReport, CrashLog.crash_id == CrashReport.id)
            .outerjoin(Issue, CrashReport.issue_id == Issue.id)
            .where(
                CrashLog.zst_path.is_not(None),
                CrashReport.status.notin_(ACTIVE_CRASH_STATUSES),
            )
            .order_by(CrashReport.created_at.asc(), CrashLog.id.asc())
            .limit(GC_BATCH_SIZE)
        )
        if cursor is not None:
            l_ts, l_id = cursor
            stmt = stmt.where(
                or_(
                    CrashReport.created_at > l_ts,
                    and_(CrashReport.created_at == l_ts, CrashLog.id > l_id),
                )
            )
        rows = (await session.execute(stmt)).all()
        if not rows:
            break
        cursor = (rows[-1][4], rows[-1][0])

        for log_id, crash_id, zst_path, idx_path, _created_at, is_pinned, issue_pinned in rows:
            if (
                logs_used <= logs_target
                and fs_used <= total_target
                and not _needs_emergency_reclaim(
                    storage, force_emergency=force_emergency, emergency=emergency
                )
            ):
                return
            if is_pinned or bool(issue_pinned):
                stats.skipped_pinned_logs.add(log_id)
                continue
            if zst_path and await _claim_log_eviction(session, log_id, crash_id, zst_path):
                await session.commit()
                freed, deleted = _unlink_log_paths(storage, logs_root, zst_path, idx_path)
                stats.freed_bytes += freed
                stats.deleted_files_count += deleted
                logs_used = max(0, logs_used - freed)
                fs_used = max(0, fs_used - freed)

        if len(rows) < GC_BATCH_SIZE:
            break


async def _evict_sym_lru(
    storage: StorageManager,
    session: AsyncSession,
    stats: _GCStats,
    *,
    raw_root: Path,
    threshold_ratio: float,
    total_target: int,
) -> None:
    sym_root = storage.partition_path("symbols/sym")
    sym_used, _ = await asyncio.to_thread(storage.partition_used_bytes, "symbols/sym")
    sym_target = int(storage.get_partition_quota_bytes("symbols/sym") * threshold_ratio)
    fs_used = await asyncio.to_thread(
        lambda: sum(storage.partition_used_bytes(p)[0] for p in PARTITIONS)
    )
    if sym_used <= sym_target and fs_used <= total_target:
        return

    async with storage.project_sym_lock(exclusive=True):
        async with storage.raw_symbol_lock(exclusive=True):
            sym_used, _ = await asyncio.to_thread(storage.partition_used_bytes, "symbols/sym")
            fs_used = await asyncio.to_thread(
                lambda: sum(storage.partition_used_bytes(p)[0] for p in PARTITIONS)
            )
            if sym_used <= sym_target and fs_used <= total_target:
                return

            syms_lru_stmt = (
                select(Symbol)
                .where(Symbol.project_id.is_not(None))
                .order_by(Symbol.last_used_at.asc())
            )
            all_syms = (await session.execute(syms_lru_stmt)).scalars().all()
            path_counts: dict[str, int] = {}
            raw_counts: dict[str, int] = {}
            pe_counts: dict[tuple[str, str], int] = {}
            for s in all_syms:
                path_counts[s.sym_path] = path_counts.get(s.sym_path, 0) + 1
                if s.raw_archive_path:
                    raw_counts[s.raw_archive_path] = raw_counts.get(s.raw_archive_path, 0) + 1
                if s.code_id and s.code_file and s.has_pe_unwind:
                    pe_key = (s.code_id, s.code_file)
                    pe_counts[pe_key] = pe_counts.get(pe_key, 0) + 1

            for sym in all_syms:
                if sym_used <= sym_target and fs_used <= total_target:
                    break

                unlink_sym: str | None = None
                path_counts[sym.sym_path] = path_counts.get(sym.sym_path, 1) - 1
                if path_counts[sym.sym_path] <= 0:
                    unlink_sym = sym.sym_path

                pe_raw_rel: str | None = None
                if sym.code_id and sym.code_file and sym.has_pe_unwind:
                    pe_key = (sym.code_id, sym.code_file)
                    pe_counts[pe_key] = pe_counts.get(pe_key, 1) - 1
                    pe_raw_rel = f"symbols/raw/{sym.code_id}/{sym.code_file}.zst"

                unlink_raws: list[str] = []
                if sym.raw_archive_path:
                    raw_rel = sym.raw_archive_path
                    raw_counts[raw_rel] = raw_counts.get(raw_rel, 1) - 1
                    pe_still_referenced = (
                        pe_raw_rel is not None
                        and raw_rel == pe_raw_rel
                        and sym.code_id is not None
                        and sym.code_file is not None
                        and pe_counts.get((sym.code_id, sym.code_file), 0) > 0
                    )
                    if raw_counts[raw_rel] <= 0 and not pe_still_referenced:
                        unlink_raws.append(raw_rel)

                if (
                    pe_raw_rel is not None
                    and pe_raw_rel != sym.raw_archive_path
                    and sym.code_id is not None
                    and sym.code_file is not None
                    and pe_counts.get((sym.code_id, sym.code_file), 0) <= 0
                    and raw_counts.get(pe_raw_rel, 0) <= 0
                ):
                    unlink_raws.append(pe_raw_rel)

                await session.delete(sym)
                await session.commit()

                if unlink_sym:
                    try:
                        target = storage.resolve_path(unlink_sym)
                        removed = storage._safe_unlink(target)
                        if removed > 0:
                            stats.freed_bytes += removed
                            stats.deleted_files_count += 1
                            sym_used = max(0, sym_used - removed)
                            fs_used = max(0, fs_used - removed)
                        storage._prune_empty_dirs(target.parent, sym_root)
                    except StorageError:
                        pass

                for raw_rel in unlink_raws:
                    try:
                        raw_target_path = storage.resolve_path(raw_rel)
                        removed_raw = storage._safe_unlink(raw_target_path)
                        if removed_raw > 0:
                            stats.freed_bytes += removed_raw
                            stats.deleted_files_count += 1
                            fs_used = max(0, fs_used - removed_raw)
                        storage._prune_empty_dirs(raw_target_path.parent, raw_root)
                    except StorageError:
                        pass


async def run_storage_gc(
    storage: StorageManager,
    session: AsyncSession,
    force_emergency: bool = False,
) -> dict[str, Any]:
    """Execute one bounded TTL + LRU GC pass across all storage partitions."""
    storage.ensure_directories()
    now = datetime.now(UTC)
    emergency = force_emergency or storage.is_host_floor_breached()
    threshold_ratio = storage.settings.GC_THRESHOLD_PERCENT / 100.0
    stats = _GCStats()

    # Step 1: staging TTL / emergency cleanup.
    # Order matters: inspect POSIX upload leases FIRST, then query fresh active crash IDs in DB
    # and queued symbol jobs so an upload releasing its lease after commit/enqueue is protected.
    staging_root, staging_candidates, staging_used = await asyncio.to_thread(
        collect_evictable_staging_candidates,
        storage,
    )
    active_crash_ids = await load_active_upload_crash_ids(session)
    active_symbol_upload_ids = await load_active_queued_symbol_upload_ids(storage, staging_root)
    await asyncio.to_thread(
        unlink_evictable_staging_candidates,
        storage,
        stats,
        staging_root=staging_root,
        candidates=staging_candidates,
        staging_used=staging_used,
        now=now,
        threshold_ratio=threshold_ratio,
        force_emergency=force_emergency,
        emergency=emergency,
        active_crash_ids=active_crash_ids,
        active_symbol_upload_ids=active_symbol_upload_ids,
    )

    # Step 2: TTL eviction for dumps & logs (protecting pinned & active items)
    dumps_root, logs_root = await _evict_expired_dumps_and_logs(
        storage, session, stats, now=now
    )

    # Step 3: Bounded single-pass quota / emergency LRU eviction
    await _evict_public_cache(
        storage,
        session,
        stats,
        threshold_ratio=threshold_ratio,
        force_emergency=force_emergency,
        emergency=emergency,
    )
    raw_root = await _evict_raw_symbols(
        storage,
        session,
        stats,
        threshold_ratio=threshold_ratio,
        force_emergency=force_emergency,
        emergency=emergency,
    )

    total_quota_target = int(
        storage.get_partition_quota_bytes("symbols/sym")
        + storage.get_partition_quota_bytes("symbols/public_cache")
        + storage.get_partition_quota_bytes("symbols/raw")
        + storage.get_partition_quota_bytes("logs")
        + storage.get_partition_quota_bytes("dumps")
        + storage.get_partition_quota_bytes("staging")
    )
    db_used = await storage.get_db_size_bytes(session)
    total_target = max(
        0,
        min(
            int(total_quota_target * threshold_ratio),
            int(storage.settings.TOTAL_DISK_QUOTA_GB * GIB * threshold_ratio) - db_used,
        ),
    )

    await _evict_dumps_lru(
        storage,
        session,
        stats,
        dumps_root=dumps_root,
        threshold_ratio=threshold_ratio,
        total_target=total_target,
        force_emergency=force_emergency,
        emergency=emergency,
    )
    await _evict_logs_lru(
        storage,
        session,
        stats,
        logs_root=logs_root,
        threshold_ratio=threshold_ratio,
        total_target=total_target,
        force_emergency=force_emergency,
        emergency=emergency,
    )
    await _evict_sym_lru(
        storage,
        session,
        stats,
        raw_root=raw_root,
        threshold_ratio=threshold_ratio,
        total_target=total_target,
    )

    async with storage.storage_io_lock(exclusive=True):
        scanned_after = await storage.reconcile_usage(session)
        await session.commit()

    db_used = await storage.get_db_size_bytes(session)
    report_after = storage._format_disk_usage_report(scanned_after, db_used_bytes=db_used)
    skipped_pinned_count = len(stats.skipped_pinned_dumps) + len(stats.skipped_pinned_logs)

    return {
        "freed_bytes": stats.freed_bytes,
        "deleted_files_count": stats.deleted_files_count,
        "skipped_pinned_count": skipped_pinned_count,
        "emergency_triggered": emergency,
        "partitions": report_after["partitions"],
    }
