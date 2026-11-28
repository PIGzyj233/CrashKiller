"""Shared helpers for symbol upload sanitization, file pruning, and resymbolication queuing."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CrashReport
from app.services import symbol_service
from app.services.missing_symbols import (
    backfill_unindexed_missing_symbols,
    build_missing_filter_clause,
)
from app.services.storage import StorageError, StorageManager
from app.workers.jobs import ACTIVE_CRASH_STATUSES, crash_has_job, stage_crash_job
from app.workers.worker import TaskDispatcher


def _sanitize_upload_basename(filename: str | None, fallback: str) -> str:
    if not filename:
        return fallback
    name = Path(filename.replace("\\", "/")).name.strip()
    if not name:
        return fallback
    if name in {".", ".."} or "\x00" in name or ".." in name:
        return name
    return symbol_service.clamp_upload_filename(name, max_bytes=240)


def _unlink_and_prune(storage: StorageManager, rel_path: str, partition: str) -> int:
    try:
        abs_path = storage.resolve_path(rel_path)
        part_root = storage.partition_path(partition).resolve()
    except StorageError:
        return 0
    if not abs_path.is_relative_to(part_root) or abs_path == part_root:
        return 0
    try:
        removed = storage._safe_unlink(abs_path)
    except OSError:
        return 0
    current = abs_path.parent.resolve()
    while current != part_root and current.is_relative_to(part_root):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent
    return removed


async def _queue_resymbolication_with_stats(
    *,
    session: AsyncSession,
    dispatcher: TaskDispatcher,
    storage: StorageManager,
    project_id: uuid.UUID | None = None,
    debug_ids: set[str] | None = None,
    crash_ids: list[uuid.UUID] | None = None,
    missing_only: bool = False,
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Reserve eligible idle crashes and enqueue durable resymbolication jobs.

    Returns (queued_crash_ids, skipped_active_crash_ids).
    """
    if crash_ids is not None and len(crash_ids) == 0:
        return [], []
    norm_debug_ids = {d.strip().upper() for d in (debug_ids or set()) if d and d.strip()}
    if debug_ids is not None and not norm_debug_ids:
        return [], []

    _ = storage
    active_ids: list[uuid.UUID] = []
    if missing_only or norm_debug_ids:
        await backfill_unindexed_missing_symbols(
            session,
            project_id=project_id,
            commit_per_batch=True,
        )

    stmt = select(
        CrashReport.id,
        CrashReport.status,
        crash_has_job().label("has_job"),
    ).where(CrashReport.dump_path.is_not(None))
    if crash_ids is not None:
        stmt = stmt.where(CrashReport.id.in_(crash_ids))
    if project_id is not None:
        stmt = stmt.where(CrashReport.project_id == project_id)
    if missing_only or norm_debug_ids:
        stmt = stmt.where(build_missing_filter_clause(norm_debug_ids or None))

    rows = (await session.execute(stmt)).all()
    matched_ids: list[uuid.UUID] = []
    for row_id, row_status, has_job in rows:
        if row_status in ACTIVE_CRASH_STATUSES or bool(has_job):
            active_ids.append(row_id)
            continue
        matched_ids.append(row_id)

    queued_ids: list[uuid.UUID] = []
    job_ids: list[uuid.UUID] = []
    for start in range(0, len(matched_ids), 100):
        batch = matched_ids[start : start + 100]
        reserved = list(
            await session.scalars(
                update(CrashReport)
                .where(
                    CrashReport.id.in_(batch),
                    CrashReport.dump_path.is_not(None),
                    CrashReport.status.notin_(ACTIVE_CRASH_STATUSES),
                    ~crash_has_job(),
                )
                .values(status="PENDING")
                .returning(CrashReport.id)
            )
        )
        reserved_set = set(reserved)
        if len(reserved_set) < len(batch):
            for cid in batch:
                if cid not in reserved_set:
                    active_ids.append(cid)
        for crash_id in reserved:
            queued_ids.append(crash_id)
            job_ids.append(stage_crash_job(session, crash_id, "resymbolicate"))

    if queued_ids:
        await session.commit()

    if job_ids:
        await dispatcher.kick_crash_jobs(
            job_ids,
            task_by_job_id={jid: "resymbolicate" for jid in job_ids},
        )
    return queued_ids, active_ids


async def _queue_resymbolication(
    *,
    session: AsyncSession,
    dispatcher: TaskDispatcher,
    storage: StorageManager,
    project_id: uuid.UUID | None = None,
    debug_ids: set[str] | None = None,
    crash_ids: list[uuid.UUID] | None = None,
    missing_only: bool = False,
) -> list[uuid.UUID]:
    queued_ids, _ = await _queue_resymbolication_with_stats(
        session=session,
        dispatcher=dispatcher,
        storage=storage,
        project_id=project_id,
        debug_ids=debug_ids,
        crash_ids=crash_ids,
        missing_only=missing_only,
    )
    return queued_ids
