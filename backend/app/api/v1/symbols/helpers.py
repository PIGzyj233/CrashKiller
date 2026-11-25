"""Shared helpers for symbol upload sanitization, file pruning, and resymbolication queuing."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CrashReport
from app.services import symbol_service
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


def _unlink_and_prune(storage: StorageManager, rel_path: str, partition: str) -> None:
    try:
        abs_path = storage.resolve_path(rel_path)
        part_root = storage.partition_path(partition).resolve()
    except StorageError:
        return
    if not abs_path.is_relative_to(part_root) or abs_path == part_root:
        return
    try:
        abs_path.unlink(missing_ok=True)
    except OSError:
        return
    current = abs_path.parent.resolve()
    while current != part_root and current.is_relative_to(part_root):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


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

    active_ids: list[uuid.UUID] = []
    async with storage.shared_lock("capacity"):
        stmt = select(
            CrashReport.id,
            CrashReport.status,
            CrashReport.missing_symbols_json,
            crash_has_job().label("has_job"),
        ).where(CrashReport.dump_path.is_not(None))
        if crash_ids is not None:
            stmt = stmt.where(CrashReport.id.in_(crash_ids))
        if project_id is not None:
            stmt = stmt.where(CrashReport.project_id == project_id)

        rows = (await session.execute(stmt)).all()
        matched_ids: list[uuid.UUID] = []
        for row_id, row_status, missing_json, has_job in rows:
            if missing_only or norm_debug_ids:
                if not any(
                    isinstance(entry, dict)
                    and (
                        not norm_debug_ids
                        or str(entry.get("debug_id") or "").strip().upper() in norm_debug_ids
                    )
                    for entry in (missing_json or [])
                ):
                    continue
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
        await dispatcher.kick_crash_jobs(job_ids)
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
