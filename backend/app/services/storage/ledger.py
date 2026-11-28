"""Database-backed incremental partition usage ledger and O(1) quota admission."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StoragePartitionUsage
from app.services.storage.constants import GIB, PARTITIONS, StorageCapacityError, StorageError

if TYPE_CHECKING:
    from app.services.storage.manager import StorageManager

SORTED_PARTITIONS: tuple[str, ...] = tuple(sorted(PARTITIONS))


def _is_postgresql(session: AsyncSession) -> bool:
    bind = session.get_bind()
    return bind is not None and bind.dialect.name == "postgresql"


def _validate_partition_keys(mapping: Mapping[str, int]) -> None:
    for part in mapping:
        if part not in PARTITIONS:
            raise StorageError(f"Unknown storage partition: {part}")


async def _scan_partitions_on_disk(
    storage: StorageManager,
    partitions: tuple[str, ...],
) -> dict[str, tuple[int, int]]:
    return await asyncio.to_thread(
        lambda: {part: storage.ledger_partition_used_bytes(part) for part in partitions}
    )


async def ensure_ledger_rows(
    session: AsyncSession,
    storage: StorageManager,
    *,
    lock: bool = False,
) -> dict[str, StoragePartitionUsage]:
    """Ensure all 6 partition rows exist in storage_partition_usage, returning them keyed by name.

    When lock=True on PostgreSQL, rows are locked in deterministic alphabetical partition order
    to prevent deadlocks under concurrent uploads and deletions.
    """
    is_pg = _is_postgresql(session)

    def _select_stmt():
        stmt = select(StoragePartitionUsage).order_by(StoragePartitionUsage.partition.asc())
        if lock and is_pg:
            stmt = stmt.with_for_update()
        return stmt

    rows = list((await session.execute(_select_stmt())).scalars().all())
    by_part = {row.partition: row for row in rows}
    missing = tuple(part for part in SORTED_PARTITIONS if part not in by_part)
    if missing:
        scanned = await _scan_partitions_on_disk(storage, missing)
        now = datetime.now(UTC)
        for part in missing:
            used_bytes, file_count = scanned[part]
            try:
                async with session.begin_nested():
                    session.add(
                        StoragePartitionUsage(
                            partition=part,
                            used_bytes=max(0, int(used_bytes)),
                            file_count=max(0, int(file_count)),
                            reconciled_at=now,
                        )
                    )
                    await session.flush()
            except IntegrityError:
                pass
        rows = list((await session.execute(_select_stmt())).scalars().all())
        by_part = {row.partition: row for row in rows}

    unreconciled = tuple(
        part
        for part in SORTED_PARTITIONS
        if part in by_part and by_part[part].reconciled_at is None
    )
    if unreconciled:
        scanned = await _scan_partitions_on_disk(storage, unreconciled)
        now = datetime.now(UTC)
        for part in unreconciled:
            used_bytes, file_count = scanned[part]
            row = by_part[part]
            row.used_bytes = max(0, int(used_bytes))
            row.file_count = max(0, int(file_count))
            row.reconciled_at = now
        await session.flush()

    return by_part


async def check_capacity_from_ledger(
    session: AsyncSession,
    storage: StorageManager,
    additional_bytes: Mapping[str, int],
) -> None:
    """Validate partition and total storage quotas in O(1) using the usage ledger."""
    _validate_partition_keys(additional_bytes)
    db_used = await storage.get_db_size_bytes(session)
    if db_used and db_used >= storage.get_partition_quota_bytes("db"):
        raise StorageCapacityError("PostgreSQL storage quota reached")

    by_part = await ensure_ledger_rows(session, storage, lock=False)
    scratch_bytes, _ = await asyncio.to_thread(storage.active_staging_scratch_bytes)
    for partition, incoming in additional_bytes.items():
        if incoming < 0:
            continue
        used = int(by_part[partition].used_bytes) + (
            scratch_bytes if partition == "staging" else 0
        )
        if used + incoming > storage.get_partition_quota_bytes(partition):
            raise StorageCapacityError(f"Storage quota reached for {partition}")

    total_fs_used = sum(int(row.used_bytes) for row in by_part.values()) + scratch_bytes
    total_incoming = sum(v for v in additional_bytes.values() if v > 0)
    if db_used + total_fs_used + total_incoming > int(storage.settings.TOTAL_DISK_QUOTA_GB * GIB):
        raise StorageCapacityError("CrashKiller total storage quota reached")


async def commit_partition_deltas(
    session: AsyncSession,
    storage: StorageManager,
    byte_deltas: Mapping[str, int],
    file_deltas: Mapping[str, int] | None = None,
    *,
    enforce_quota: bool = True,
    extra_headroom_checks: Mapping[str, int] | None = None,
    check_partitions: Iterable[str] | None = None,
) -> None:
    """Atomically verify quotas (when enforcing) and apply incremental deltas to the usage ledger.

    Caller owns transaction commit/rollback; row locks on PostgreSQL are held only until commit.
    """
    _validate_partition_keys(byte_deltas)
    if file_deltas:
        _validate_partition_keys(file_deltas)
    if extra_headroom_checks:
        _validate_partition_keys(extra_headroom_checks)
    checked_parts = tuple(check_partitions or ())
    if checked_parts:
        _validate_partition_keys({part: 0 for part in checked_parts})

    has_positive_growth = (
        any(delta > 0 for delta in byte_deltas.values())
        or bool(extra_headroom_checks)
        or bool(checked_parts)
    )
    by_part = await ensure_ledger_rows(session, storage, lock=True)

    projected: dict[str, int] = {
        part: max(0, int(by_part[part].used_bytes) + int(byte_deltas.get(part, 0)))
        for part in SORTED_PARTITIONS
    }

    if enforce_quota and has_positive_growth:
        db_used = await storage.get_db_size_bytes(session)
        if db_used and db_used >= storage.get_partition_quota_bytes("db"):
            raise StorageCapacityError("PostgreSQL storage quota reached")

        scratch_bytes, _ = await asyncio.to_thread(storage.active_staging_scratch_bytes)

        for partition, delta in byte_deltas.items():
            eff_proj = projected[partition] + (scratch_bytes if partition == "staging" else 0)
            if delta > 0 and eff_proj > storage.get_partition_quota_bytes(partition):
                raise StorageCapacityError(f"Storage quota reached for {partition}")

        for partition in checked_parts:
            eff_proj = projected[partition] + (scratch_bytes if partition == "staging" else 0)
            if eff_proj > storage.get_partition_quota_bytes(partition):
                raise StorageCapacityError(f"Storage quota reached for {partition}")

        for partition, headroom in (extra_headroom_checks or {}).items():
            eff_proj = projected[partition] + (scratch_bytes if partition == "staging" else 0)
            if headroom > 0 and (
                eff_proj + headroom > storage.get_partition_quota_bytes(partition)
            ):
                raise StorageCapacityError(f"Storage quota reached for {partition}")

        if db_used + sum(projected.values()) + scratch_bytes > int(
            storage.settings.TOTAL_DISK_QUOTA_GB * GIB
        ):
            raise StorageCapacityError("CrashKiller total storage quota reached")

    fdeltas = file_deltas or {}
    for partition in SORTED_PARTITIONS:
        bdelta = int(byte_deltas.get(partition, 0))
        fdelta = int(fdeltas.get(partition, 0))
        if bdelta == 0 and fdelta == 0:
            continue
        row = by_part[partition]
        row.used_bytes = max(0, int(row.used_bytes) + bdelta)
        row.file_count = max(0, int(row.file_count) + fdelta)
    await session.flush()


async def reserve_staging_scratch_capacity(
    session: AsyncSession,
    storage: StorageManager,
    staging_dir: Path,
    reserved_bytes: int,
) -> None:
    """Atomically admit and record a scratch byte budget for an active staging_workspace().

    Serializes against concurrent crash/symbol staging reservations using the same
    ``storage_partition_usage`` row locks, then writes ``.tmp_reserved_bytes`` inside the
    flocked workspace before releasing the lock. If the process crashes, the OS releases the
    workspace flock and GC cleans up the directory without leaking persistent ledger bytes.
    """
    incoming = max(0, int(reserved_bytes))
    by_part = await ensure_ledger_rows(session, storage, lock=True)
    db_used = await storage.get_db_size_bytes(session)
    if db_used and db_used >= storage.get_partition_quota_bytes("db"):
        raise StorageCapacityError("PostgreSQL storage quota reached")

    scratch_bytes, _ = await asyncio.to_thread(storage.active_staging_scratch_bytes)
    staging_used = int(by_part["staging"].used_bytes) + scratch_bytes
    if staging_used + incoming > storage.get_partition_quota_bytes("staging"):
        raise StorageCapacityError("Storage quota reached for staging")

    total_fs_used = sum(int(row.used_bytes) for row in by_part.values()) + scratch_bytes
    if db_used + total_fs_used + incoming > int(storage.settings.TOTAL_DISK_QUOTA_GB * GIB):
        raise StorageCapacityError("CrashKiller total storage quota reached")

    marker = staging_dir / ".tmp_reserved_bytes"
    marker.write_text(str(incoming), encoding="utf-8")


async def reconcile_single_partition_usage(
    session: AsyncSession,
    storage: StorageManager,
    partition: str,
) -> tuple[int, int]:
    """Re-scan one partition under ledger row lock and update storage_partition_usage."""
    _validate_partition_keys({partition: 0})
    by_part = await ensure_ledger_rows(session, storage, lock=True)
    used_bytes, file_count = await asyncio.to_thread(
        storage.ledger_partition_used_bytes, partition
    )
    row = by_part[partition]
    row.used_bytes = max(0, int(used_bytes))
    row.file_count = max(0, int(file_count))
    row.reconciled_at = datetime.now(UTC)
    await session.flush()
    return used_bytes, file_count


async def reconcile_partition_usage(
    session: AsyncSession,
    storage: StorageManager,
) -> dict[str, tuple[int, int]]:
    """Re-scan disk partitions under exclusive storage-io lock + ledger row locks.

    Acquiring the ledger row locks BEFORE scanning disk (while caller or this function holds
    storage_io_lock(exclusive=True)) guarantees no concurrent upload can commit after the scan
    and have its delta overwritten.
    """
    by_part = await ensure_ledger_rows(session, storage, lock=True)
    scanned = await _scan_partitions_on_disk(storage, SORTED_PARTITIONS)
    now = datetime.now(UTC)
    for partition in SORTED_PARTITIONS:
        used_bytes, file_count = scanned[partition]
        row = by_part[partition]
        row.used_bytes = max(0, int(used_bytes))
        row.file_count = max(0, int(file_count))
        row.reconciled_at = now
    await session.flush()
    return scanned


async def build_disk_usage_report_from_ledger(
    session: AsyncSession,
    storage: StorageManager,
    db_used_bytes: int = 0,
) -> dict[str, Any]:
    """Build the disk usage report from the O(1) usage ledger plus host statvfs."""
    storage.ensure_directories()
    by_part = await ensure_ledger_rows(session, storage, lock=False)
    scratch_bytes, scratch_files = await asyncio.to_thread(storage.active_staging_scratch_bytes)
    partition_stats = {
        part: (
            int(by_part[part].used_bytes) + (scratch_bytes if part == "staging" else 0),
            int(by_part[part].file_count) + (scratch_files if part == "staging" else 0),
        )
        for part in PARTITIONS
    }
    return storage._format_disk_usage_report(partition_stats, db_used_bytes=db_used_bytes)
