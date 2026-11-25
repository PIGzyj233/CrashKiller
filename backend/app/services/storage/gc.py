"""TTL and quota/emergency LRU garbage collection for storage partitions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CrashLog, CrashReport, Issue, Project, Symbol
from app.services.storage.constants import ACTIVE_CRASH_STATUSES, GIB, PARTITIONS, StorageError

if TYPE_CHECKING:
    from app.services.storage.manager import StorageManager


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


def _evict_staging(
    storage: StorageManager,
    stats: _GCStats,
    *,
    now: datetime,
    threshold_ratio: float,
    force_emergency: bool,
    emergency: bool,
) -> None:
    staging_root = storage.partition_path("staging")
    staging_cutoff_ts = (now - timedelta(hours=storage.settings.STAGING_TTL_HOURS)).timestamp()
    staging_files = storage._iter_files_sorted_by_age(staging_root)
    staging_used = sum(sz for _, _, sz in staging_files)
    staging_target = int(storage.get_partition_quota_bytes("staging") * threshold_ratio)

    for ts, path, size in staging_files:
        workspace = path.relative_to(staging_root).parts[0]
        if storage._staging_in_use(staging_root, workspace):
            continue
        needs_reclaim = _needs_emergency_reclaim(
            storage, force_emergency=force_emergency, emergency=emergency
        )
        if ts < staging_cutoff_ts or needs_reclaim or staging_used > staging_target:
            removed = storage._safe_unlink(path)
            if removed > 0 or not path.exists():
                stats.freed_bytes += removed
                stats.deleted_files_count += 1
                staging_used = max(0, staging_used - size)
                storage._prune_empty_dirs(path.parent, staging_root)


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

    dumps_root = storage.partition_path("dumps")
    candidate_dumps_stmt = (
        select(CrashReport, Issue)
        .outerjoin(Issue, CrashReport.issue_id == Issue.id)
        .where(CrashReport.dump_path.is_not(None))
        .order_by(CrashReport.created_at.asc())
    )
    for report, issue in (await session.execute(candidate_dumps_stmt)).all():
        if report.status in ACTIVE_CRASH_STATUSES:
            continue
        dump_cutoff = retention_by_project.get(
            report.project_id,
            (default_dump_cutoff, default_log_cutoff),
        )[0]
        if _as_utc(report.created_at) >= dump_cutoff:
            continue
        if report.is_pinned or (issue is not None and issue.is_pinned):
            stats.skipped_pinned_dumps.add(report.id)
            continue
        if report.dump_path:
            try:
                target = storage.resolve_path(report.dump_path)
                removed = storage._safe_unlink(target)
                if removed > 0:
                    stats.freed_bytes += removed
                    stats.deleted_files_count += 1
                storage._prune_empty_dirs(target.parent, dumps_root)
            except StorageError:
                pass
            report.dump_path = None

    logs_root = storage.partition_path("logs")
    candidate_logs_stmt = (
        select(CrashLog, CrashReport, Issue)
        .join(CrashReport, CrashLog.crash_id == CrashReport.id)
        .outerjoin(Issue, CrashReport.issue_id == Issue.id)
        .where(CrashLog.zst_path.is_not(None))
        .order_by(CrashReport.created_at.asc())
    )
    for log_row, report, issue in (await session.execute(candidate_logs_stmt)).all():
        if report.status in ACTIVE_CRASH_STATUSES:
            continue
        log_cutoff = retention_by_project.get(
            report.project_id,
            (default_dump_cutoff, default_log_cutoff),
        )[1]
        if _as_utc(report.created_at) >= log_cutoff:
            continue
        if report.is_pinned or (issue is not None and issue.is_pinned):
            stats.skipped_pinned_logs.add(log_row.id)
            continue
        for rel_attr in (log_row.zst_path, log_row.idx_path):
            if rel_attr:
                try:
                    target = storage.resolve_path(rel_attr)
                    removed = storage._safe_unlink(target)
                    if removed > 0:
                        stats.freed_bytes += removed
                        stats.deleted_files_count += 1
                    storage._prune_empty_dirs(target.parent, logs_root)
                except StorageError:
                    pass
        log_row.zst_path = None
        log_row.idx_path = None

    await session.flush()
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
    pub_used, _ = storage.partition_used_bytes("symbols/public_cache")
    pub_target = int(storage.get_partition_quota_bytes("symbols/public_cache") * threshold_ratio)
    if pub_used <= pub_target and not _needs_emergency_reclaim(
        storage, force_emergency=force_emergency, emergency=emergency
    ):
        return

    pub_syms_stmt = select(Symbol).where(Symbol.project_id.is_(None))
    pub_syms = (await session.execute(pub_syms_stmt)).scalars().all()
    pub_by_path: dict[str, Symbol] = {s.sym_path: s for s in pub_syms if s.sym_path}
    data_root = storage.data_dir.resolve()

    eviction_entries: list[tuple[int, float, Path, int, Symbol | None]] = []
    for mtime, path, size in storage._iter_files_sorted_by_age(pub_root):
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
        removed = storage._safe_unlink(path)
        if removed > 0:
            stats.freed_bytes += removed
            stats.deleted_files_count += 1
            pub_used = max(0, pub_used - size)
            storage._prune_empty_dirs(path.parent, pub_root)
        if sym_row is not None:
            await session.delete(sym_row)


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
    raw_used, _ = storage.partition_used_bytes("symbols/raw")
    raw_target = int(storage.get_partition_quota_bytes("symbols/raw") * threshold_ratio)
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
                try:
                    target = storage.resolve_path(orig_raw_rel)
                    removed = storage._safe_unlink(target)
                    if removed > 0:
                        stats.freed_bytes += removed
                        stats.deleted_files_count += 1
                        raw_used = max(0, raw_used - removed)
                    storage._prune_empty_dirs(target.parent, raw_root)
                except StorageError:
                    pass

        if (
            pe_raw_rel is not None
            and pe_raw_rel != orig_raw_rel
            and sym.code_id is not None
            and sym.code_file is not None
            and pe_counts.get((sym.code_id, sym.code_file), 0) <= 0
            and raw_counts.get(pe_raw_rel, 0) <= 0
        ):
            try:
                pe_target = storage.resolve_path(pe_raw_rel)
                removed_pe = storage._safe_unlink(pe_target)
                if removed_pe > 0:
                    stats.freed_bytes += removed_pe
                    stats.deleted_files_count += 1
                    raw_used = max(0, raw_used - removed_pe)
                storage._prune_empty_dirs(pe_target.parent, raw_root)
            except StorageError:
                pass

        sym.raw_archive_path = None
        sym.raw_size = 0

    # Sweep any remaining untracked raw files if still above target
    if raw_used > raw_target or _needs_emergency_reclaim(
        storage, force_emergency=force_emergency, emergency=emergency
    ):
        for _, path, size in storage._iter_files_sorted_by_age(raw_root):
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
    dumps_used, _ = storage.partition_used_bytes("dumps")
    dumps_target = int(storage.get_partition_quota_bytes("dumps") * threshold_ratio)
    fs_used = sum(storage.partition_used_bytes(p)[0] for p in PARTITIONS)
    if (
        not _needs_emergency_reclaim(storage, force_emergency=force_emergency, emergency=emergency)
        and dumps_used <= dumps_target
        and fs_used <= total_target
    ):
        return

    dumps_lru_stmt = (
        select(CrashReport, Issue)
        .outerjoin(Issue, CrashReport.issue_id == Issue.id)
        .where(CrashReport.dump_path.is_not(None))
        .order_by(CrashReport.created_at.asc())
    )
    for report, issue in (await session.execute(dumps_lru_stmt)).all():
        if (
            dumps_used <= dumps_target
            and fs_used <= total_target
            and not _needs_emergency_reclaim(
                storage, force_emergency=force_emergency, emergency=emergency
            )
        ):
            break
        if report.is_pinned or (issue is not None and issue.is_pinned):
            stats.skipped_pinned_dumps.add(report.id)
            continue
        if report.status in ACTIVE_CRASH_STATUSES:
            continue
        if report.dump_path:
            try:
                target = storage.resolve_path(report.dump_path)
                removed = storage._safe_unlink(target)
                if removed > 0:
                    stats.freed_bytes += removed
                    stats.deleted_files_count += 1
                    dumps_used = max(0, dumps_used - removed)
                    fs_used = max(0, fs_used - removed)
                storage._prune_empty_dirs(target.parent, dumps_root)
            except StorageError:
                pass
            report.dump_path = None


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
    logs_used, _ = storage.partition_used_bytes("logs")
    logs_target = int(storage.get_partition_quota_bytes("logs") * threshold_ratio)
    fs_used = sum(storage.partition_used_bytes(p)[0] for p in PARTITIONS)
    if (
        not _needs_emergency_reclaim(storage, force_emergency=force_emergency, emergency=emergency)
        and logs_used <= logs_target
        and fs_used <= total_target
    ):
        return

    logs_lru_stmt = (
        select(CrashLog, CrashReport, Issue)
        .join(CrashReport, CrashLog.crash_id == CrashReport.id)
        .outerjoin(Issue, CrashReport.issue_id == Issue.id)
        .where(CrashLog.zst_path.is_not(None))
        .order_by(CrashReport.created_at.asc())
    )
    for log_row, report, issue in (await session.execute(logs_lru_stmt)).all():
        if (
            logs_used <= logs_target
            and fs_used <= total_target
            and not _needs_emergency_reclaim(
                storage, force_emergency=force_emergency, emergency=emergency
            )
        ):
            break
        if report.is_pinned or (issue is not None and issue.is_pinned):
            stats.skipped_pinned_logs.add(log_row.id)
            continue
        if report.status in ACTIVE_CRASH_STATUSES:
            continue
        for rel_attr in (log_row.zst_path, log_row.idx_path):
            if rel_attr:
                try:
                    target = storage.resolve_path(rel_attr)
                    removed = storage._safe_unlink(target)
                    if removed > 0:
                        stats.freed_bytes += removed
                        stats.deleted_files_count += 1
                        logs_used = max(0, logs_used - removed)
                        fs_used = max(0, fs_used - removed)
                    storage._prune_empty_dirs(target.parent, logs_root)
                except StorageError:
                    pass
        log_row.zst_path = None
        log_row.idx_path = None


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
    sym_used, _ = storage.partition_used_bytes("symbols/sym")
    sym_target = int(storage.get_partition_quota_bytes("symbols/sym") * threshold_ratio)
    fs_used = sum(storage.partition_used_bytes(p)[0] for p in PARTITIONS)
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
        path_counts[sym.sym_path] = path_counts.get(sym.sym_path, 1) - 1
        if path_counts[sym.sym_path] <= 0:
            try:
                target = storage.resolve_path(sym.sym_path)
                removed = storage._safe_unlink(target)
                if removed > 0:
                    stats.freed_bytes += removed
                    stats.deleted_files_count += 1
                    sym_used = max(0, sym_used - removed)
                    fs_used = max(0, fs_used - removed)
                storage._prune_empty_dirs(target.parent, sym_root)
            except StorageError:
                pass

        pe_raw_rel: str | None = None
        if sym.code_id and sym.code_file and sym.has_pe_unwind:
            pe_key = (sym.code_id, sym.code_file)
            pe_counts[pe_key] = pe_counts.get(pe_key, 1) - 1
            pe_raw_rel = f"symbols/raw/{sym.code_id}/{sym.code_file}.zst"

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

        if (
            pe_raw_rel is not None
            and pe_raw_rel != sym.raw_archive_path
            and sym.code_id is not None
            and sym.code_file is not None
            and pe_counts.get((sym.code_id, sym.code_file), 0) <= 0
            and raw_counts.get(pe_raw_rel, 0) <= 0
        ):
            try:
                pe_target_path = storage.resolve_path(pe_raw_rel)
                removed_pe = storage._safe_unlink(pe_target_path)
                if removed_pe > 0:
                    stats.freed_bytes += removed_pe
                    stats.deleted_files_count += 1
                    fs_used = max(0, fs_used - removed_pe)
                storage._prune_empty_dirs(pe_target_path.parent, raw_root)
            except StorageError:
                pass

        await session.delete(sym)


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

    # Step 1: staging TTL (and emergency / quota cleanup)
    _evict_staging(
        storage,
        stats,
        now=now,
        threshold_ratio=threshold_ratio,
        force_emergency=force_emergency,
        emergency=emergency,
    )

    # Step 2: TTL eviction for dumps & logs (protecting pinned items)
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
    await session.flush()

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

    await session.commit()

    db_used = await storage.get_db_size_bytes(session)
    report_after = storage.get_disk_usage_report(db_used_bytes=db_used)
    skipped_pinned_count = len(stats.skipped_pinned_dumps) + len(stats.skipped_pinned_logs)

    return {
        "freed_bytes": stats.freed_bytes,
        "deleted_files_count": stats.deleted_files_count,
        "skipped_pinned_count": skipped_pinned_count,
        "emergency_triggered": emergency,
        "partitions": report_after["partitions"],
    }
