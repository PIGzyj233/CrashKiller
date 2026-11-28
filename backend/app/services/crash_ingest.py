"""Async crash upload staging and worker Stage-0 dump/log materialization."""

from __future__ import annotations

import asyncio
import inspect
import shutil
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import CrashLog, CrashReport
from app.services.log_engine import LogEngine
from app.services.storage import StorageError, StorageManager

STAGED_UPLOADS_PREFIX = "staging/uploads/"


def is_staged_upload_path(rel_path: str | None) -> bool:
    """Return True when rel_path points inside the async upload staging area."""
    return bool(rel_path and rel_path.startswith(STAGED_UPLOADS_PREFIX))


@dataclass(slots=True)
class StagedLogItem:
    log_id: uuid.UUID
    filename: str
    rel_raw_path: str
    raw_size: int


@dataclass(slots=True)
class StagedCrashUpload:
    dump_rel_path: str
    dump_raw_size: int
    logs: list[StagedLogItem]
    total_staged_bytes: int
    total_staged_files: int
    staging_dir: Path


def cleanup_staged_upload_dir(
    storage: StorageManager,
    crash_id: uuid.UUID | str,
) -> tuple[int, int]:
    """Delete staging/uploads/<crash_id>, returning (freed_bytes, deleted_files)."""
    try:
        upload_dir = storage.resolve_path(f"{STAGED_UPLOADS_PREFIX}{crash_id}")
        staging_root = storage.partition_path("staging").resolve()
    except StorageError:
        return 0, 0
    if not upload_dir.exists() or not upload_dir.is_relative_to(staging_root):
        return 0, 0

    freed_bytes = 0
    deleted_files = 0
    for path in list(upload_dir.rglob("*")):
        if path.is_file() or path.is_symlink():
            removed = storage._safe_unlink(path)
            freed_bytes += removed
            deleted_files += 1
    shutil.rmtree(upload_dir, ignore_errors=True)
    storage._prune_empty_dirs(upload_dir.parent, staging_root)
    return freed_bytes, deleted_files


def stage_crash_upload_files(
    storage: StorageManager,
    crash_id: uuid.UUID,
    dump_stream: BinaryIO,
    log_inputs: list[tuple[uuid.UUID, str, BinaryIO]],
) -> StagedCrashUpload:
    """Stream raw dump and log uploads to staging/uploads/<crash_id>/ without blocking locks."""
    staging_dir = storage.resolve_path(f"{STAGED_UPLOADS_PREFIX}{crash_id}")
    total_bytes = 0
    total_files = 0
    staged_logs: list[StagedLogItem] = []

    try:
        dump_rel = f"{STAGED_UPLOADS_PREFIX}{crash_id}/dump.raw"
        _, dump_raw_size = storage.atomic_write_stream(dump_rel, dump_stream)
        total_bytes += dump_raw_size
        total_files += 1

        for log_id, safe_name, log_stream in log_inputs:
            log_rel = f"{STAGED_UPLOADS_PREFIX}{crash_id}/{log_id}.raw"
            _, raw_size = storage.atomic_write_stream(log_rel, log_stream)
            total_bytes += raw_size
            total_files += 1
            staged_logs.append(
                StagedLogItem(
                    log_id=log_id,
                    filename=safe_name,
                    rel_raw_path=log_rel,
                    raw_size=raw_size,
                )
            )
    except BaseException:
        cleanup_staged_upload_dir(storage, crash_id)
        raise

    return StagedCrashUpload(
        dump_rel_path=dump_rel,
        dump_raw_size=dump_raw_size,
        logs=staged_logs,
        total_staged_bytes=total_bytes,
        total_staged_files=total_files,
        staging_dir=staging_dir,
    )


def _supports_cancel_event(fn: Callable[..., Any]) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD or param.name == "cancel_event":
            return True
    return False


async def _run_thread_cancel_safe[T](
    cancel_event: threading.Event,
    fn: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Run blocking file I/O in a worker thread, waiting for exit before propagating cancels."""
    call_kwargs = dict(kwargs)
    if _supports_cancel_event(fn):
        call_kwargs["cancel_event"] = cancel_event
    worker_task = asyncio.create_task(asyncio.to_thread(fn, *args, **call_kwargs))
    try:
        return await asyncio.shield(worker_task)
    except BaseException:
        cancel_event.set()
        # Wait until the worker thread stops so cleanup runs strictly after it exits
        # and while storage_io_lock is still held.
        while not worker_task.done():
            try:
                await asyncio.shield(worker_task)
            except BaseException:
                pass
        raise


async def materialize_staged_crash_artifacts(
    session: AsyncSession,
    crash: CrashReport,
    storage: StorageManager,
    effective_app: Settings,
) -> bool:
    """Compress staged raw dump and index staged raw logs into permanent storage partitions.

    Returns True when any staged artifact was materialized; no-op (False) for already-compressed
    or legacy crashes.
    """
    needs_dump = is_staged_upload_path(crash.dump_path)
    staged_logs = list(
        (
            await session.execute(
                select(CrashLog)
                .where(
                    CrashLog.crash_id == crash.id,
                    CrashLog.staged_raw_path.is_not(None),
                )
                .order_by(CrashLog.id.asc())
            )
        )
        .scalars()
        .all()
    )

    if not needs_dump and not staged_logs:
        return False

    written_permanent: list[Path] = []
    staged_to_unlink: list[Path] = []
    byte_deltas: dict[str, int] = {}
    file_deltas: dict[str, int] = {}
    touched_partitions: set[str] = set()
    prev_permanent_bytes: dict[str, int] = {}
    prev_permanent_files: dict[str, int] = {}

    async with storage.storage_io_lock(exclusive=False):
        try:
            # Ensure ledger rows exist BEFORE overwriting pre-existing files from a crashed
            # prior attempt so net (new - prev) accounting never double-counts recovered files.
            await storage.ensure_usage_ledger(session, lock=False)

            if needs_dump:
                assert crash.dump_path is not None
                staged_dump_abs = storage.resolve_path(crash.dump_path)
                if not staged_dump_abs.is_file():
                    raise StorageError(f"Staged raw dump not found: {crash.dump_path}")
                staged_dump_size = staged_dump_abs.stat().st_size
                expected_dump_rel = f"dumps/{crash.id}/{crash.id}.dmp.zst"
                expected_dump_abs = storage.resolve_path(expected_dump_rel)
                prev_dump_exists = expected_dump_abs.is_file()
                prev_dump_size = expected_dump_abs.stat().st_size if prev_dump_exists else 0
                prev_dump_files = 1 if prev_dump_exists else 0
                if prev_dump_exists:
                    prev_permanent_bytes["dumps"] = (
                        prev_permanent_bytes.get("dumps", 0) + prev_dump_size
                    )
                    prev_permanent_files["dumps"] = prev_permanent_files.get("dumps", 0) + 1
                written_permanent.append(expected_dump_abs)
                dump_rel, dump_raw_size, dump_comp_size = await _run_thread_cancel_safe(
                    threading.Event(),
                    storage.compress_dump,
                    staged_dump_abs,
                    crash.id,
                )
                actual_dump_abs = storage.resolve_path(dump_rel)
                if actual_dump_abs not in written_permanent:
                    written_permanent.append(actual_dump_abs)
                staged_to_unlink.append(staged_dump_abs)

                crash.dump_path = dump_rel
                crash.dump_raw_size = dump_raw_size
                crash.dump_compressed_size = dump_comp_size

                touched_partitions.add("dumps")
                byte_deltas["dumps"] = byte_deltas.get("dumps", 0) + (
                    dump_comp_size - prev_dump_size
                )
                file_deltas["dumps"] = file_deltas.get("dumps", 0) + (1 - prev_dump_files)
                byte_deltas["staging"] = byte_deltas.get("staging", 0) - staged_dump_size
                file_deltas["staging"] = file_deltas.get("staging", 0) - 1

            if staged_logs:
                log_engine = LogEngine()
                for log_row in staged_logs:
                    assert log_row.staged_raw_path is not None
                    staged_log_abs = storage.resolve_path(log_row.staged_raw_path)
                    if not staged_log_abs.is_file():
                        raise StorageError(f"Staged raw log not found: {log_row.staged_raw_path}")
                    staged_log_size = staged_log_abs.stat().st_size

                    rel_zst = f"logs/{crash.id}/{log_row.id}.log.zst"
                    rel_idx = f"logs/{crash.id}/{log_row.id}.idx.json"
                    zst_abs = storage.resolve_path(rel_zst)
                    idx_abs = storage.resolve_path(rel_idx)
                    zst_abs.parent.mkdir(parents=True, exist_ok=True)
                    prev_zst_exists = zst_abs.is_file()
                    prev_idx_exists = idx_abs.is_file()
                    prev_zst_size = zst_abs.stat().st_size if prev_zst_exists else 0
                    prev_idx_size = idx_abs.stat().st_size if prev_idx_exists else 0
                    prev_log_files = (1 if prev_zst_exists else 0) + (1 if prev_idx_exists else 0)
                    if prev_log_files > 0:
                        prev_permanent_bytes["logs"] = (
                            prev_permanent_bytes.get("logs", 0) + prev_zst_size + prev_idx_size
                        )
                        prev_permanent_files["logs"] = (
                            prev_permanent_files.get("logs", 0) + prev_log_files
                        )
                    written_permanent.extend([zst_abs, idx_abs])

                    ingest_res = await _run_thread_cancel_safe(
                        threading.Event(),
                        log_engine.ingest_log,
                        staged_log_abs,
                        zst_abs,
                        idx_abs,
                        effective_app.LOG_CHUNK_SIZE_BYTES,
                        effective_app.LOG_ZSTD_LEVEL,
                        crash.session_id,
                    )
                    staged_to_unlink.append(staged_log_abs)

                    zst_disk_size = zst_abs.stat().st_size if zst_abs.is_file() else 0
                    idx_disk_size = idx_abs.stat().st_size if idx_abs.is_file() else 0
                    log_row.zst_path = rel_zst
                    log_row.idx_path = rel_idx
                    log_row.staged_raw_path = None
                    log_row.raw_size = int(ingest_res["raw_size"])
                    log_row.compressed_size = int(ingest_res["compressed_size"])
                    log_row.total_lines = int(ingest_res["total_lines"])
                    log_row.digest_json = dict(ingest_res["digest_json"])

                    touched_partitions.add("logs")
                    byte_deltas["logs"] = byte_deltas.get("logs", 0) + (
                        (zst_disk_size + idx_disk_size) - (prev_zst_size + prev_idx_size)
                    )
                    file_deltas["logs"] = file_deltas.get("logs", 0) + (2 - prev_log_files)
                    byte_deltas["staging"] = byte_deltas.get("staging", 0) - staged_log_size
                    file_deltas["staging"] = file_deltas.get("staging", 0) - 1

            if byte_deltas or file_deltas or touched_partitions:
                await storage.commit_usage_deltas(
                    session,
                    byte_deltas,
                    file_deltas,
                    enforce_quota=True,
                    check_partitions=touched_partitions,
                )
            await session.commit()
        except BaseException as exc:
            for path in written_permanent:
                storage._safe_unlink(path)
            for part_name in ("dumps", "logs"):
                part_root = storage.partition_path(part_name)
                crash_dir = part_root / str(crash.id)
                if crash_dir.exists():
                    for tmp_file in crash_dir.glob(".tmp_*"):
                        storage._safe_unlink(tmp_file)
                    storage._prune_empty_dirs(crash_dir, part_root)
            if (prev_permanent_bytes or prev_permanent_files) and isinstance(exc, Exception):
                try:
                    await session.rollback()
                    await storage.commit_usage_deltas(
                        session,
                        {k: -v for k, v in prev_permanent_bytes.items()},
                        {k: -v for k, v in prev_permanent_files.items()},
                        enforce_quota=False,
                    )
                    await session.commit()
                except Exception:
                    await session.rollback()
            raise

        # Post-commit cleanup of consumed staging raw files while still under LOCK_SH.
        staging_root = storage.partition_path("staging")
        for staged_path in staged_to_unlink:
            storage._safe_unlink(staged_path)
        cleanup_staged_upload_dir(storage, crash.id)
        storage._prune_empty_dirs(staging_root / "uploads", staging_root)
    return True
