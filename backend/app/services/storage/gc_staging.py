"""Staging partition GC candidate discovery and queued upload lease verification."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CrashJob, CrashReport
from app.services.storage.constants import ACTIVE_CRASH_STATUSES

if TYPE_CHECKING:
    from app.services.storage.manager import StorageManager

logger = logging.getLogger(__name__)


async def load_active_upload_crash_ids(session: AsyncSession) -> set[str]:
    """Return crash IDs with active statuses or durable CrashJob rows."""
    active_report_ids = (
        await session.scalars(
            select(CrashReport.id).where(CrashReport.status.in_(ACTIVE_CRASH_STATUSES))
        )
    ).all()
    active_job_crash_ids = (await session.scalars(select(CrashJob.crash_id))).all()
    return {str(cid) for cid in active_report_ids} | {str(cid) for cid in active_job_crash_ids}


async def load_active_queued_symbol_upload_ids(
    storage: StorageManager,
    staging_root: Path,
) -> set[str]:
    """Return staging/symbol_uploads/<uid> IDs whose background job is still queued or running.

    For Redis ARQ jobs, checks queue membership (`ZSCORE`), `arq:in-progress:<job_id>`,
    and `arq:job:<job_id>` so queue backlogs beyond the job execution timeout never lose inputs.
    Fails safe (keeps inputs protected) if Redis is temporarily unreachable.
    """
    sym_uploads_root = staging_root / "symbol_uploads"
    if not sym_uploads_root.is_dir():
        return set()

    protected: set[str] = set()
    redis_checks: list[tuple[str, str, str]] = []
    now_ts = time.time()

    try:
        entries = list(os.scandir(sym_uploads_root))
    except OSError:
        return protected

    from app.workers.worker import ACTIVE_MEMORY_SYMBOL_JOBS

    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        uid = entry.name
        dir_path = Path(entry.path)
        if storage._staging_upload_lease_active(staging_root, "symupload", uid):
            protected.add(uid)
            continue

        lease = storage._read_staging_queue_lease(dir_path)
        if lease is None:
            continue

        job_id = lease.get("job_id")
        if not job_id:
            # Legacy TTL-only queue lease without an associated job_id.
            try:
                if now_ts < float(lease.get("expires_at", 0.0)):
                    protected.add(uid)
            except (ValueError, TypeError):
                pass
            continue

        mode = str(lease.get("mode") or "").lower()
        if mode == "memory":
            try:
                lease_pid = int(lease.get("pid") or 0)
            except (ValueError, TypeError):
                lease_pid = 0
            if lease_pid == os.getpid() and str(job_id) in ACTIVE_MEMORY_SYMBOL_JOBS:
                protected.add(uid)
            continue

        queue_name = str(lease.get("queue_name") or "arq:queue")
        redis_checks.append((uid, str(job_id), queue_name))

    if not redis_checks:
        return protected

    redis_url = (storage.settings.REDIS_URL or "").strip()
    if not redis_url or redis_url.lower().startswith("memory://"):
        # Non-memory lease seen while running in memory:// mode: fall back to expires_at.
        for uid, _, _ in redis_checks:
            if storage._staging_queue_lease_active(sym_uploads_root / uid):
                protected.add(uid)
        return protected

    client: Any = None
    try:
        client = aioredis.from_url(redis_url, decode_responses=True)
        pipe = client.pipeline(transaction=False)
        for _, job_id, queue_name in redis_checks:
            pipe.zscore(queue_name, job_id)
            pipe.exists(f"arq:in-progress:{job_id}")
            pipe.exists(f"arq:job:{job_id}")
        results = await pipe.execute()
        for idx, (uid, _, _) in enumerate(redis_checks):
            zscore_val, in_prog, job_exists = results[idx * 3 : idx * 3 + 3]
            if zscore_val is not None or bool(in_prog) or bool(job_exists):
                protected.add(uid)
    except Exception:
        # Fail safe: do not delete queued symbol uploads when Redis status cannot be checked.
        logger.warning(
            "Could not verify queued symbol_ingest jobs in Redis; preserving leased uploads",
            exc_info=True,
        )
        for uid, _, _ in redis_checks:
            protected.add(uid)
    finally:
        if client is not None:
            try:
                aclose = getattr(client, "aclose", None)
                if callable(aclose):
                    await aclose()
                else:
                    await client.close()
            except Exception:
                pass

    return protected


def collect_evictable_staging_candidates(
    storage: StorageManager,
) -> tuple[Path, list[tuple[float, Path, int]], int]:
    """Scan staging/ and filter out directories protected by active POSIX flocks."""
    staging_root = storage.partition_path("staging")
    staging_files = storage._iter_files_sorted_by_age(staging_root)
    staging_used = sum(sz for _, _, sz in staging_files)
    candidates: list[tuple[float, Path, int]] = []
    for ts, path, size in staging_files:
        rel_parts = path.relative_to(staging_root).parts
        workspace = rel_parts[0]
        if storage._staging_in_use(staging_root, workspace):
            continue
        if workspace == "uploads" and len(rel_parts) >= 2:
            if storage._staging_upload_lease_active(staging_root, "upload", rel_parts[1]):
                continue
        elif workspace == "symbol_uploads" and len(rel_parts) >= 2:
            if storage._staging_upload_lease_active(staging_root, "symupload", rel_parts[1]):
                continue
        candidates.append((ts, path, size))
    return staging_root, candidates, staging_used


def unlink_evictable_staging_candidates(
    storage: StorageManager,
    stats: Any,
    *,
    staging_root: Path,
    candidates: list[tuple[float, Path, int]],
    staging_used: int,
    now: datetime,
    threshold_ratio: float,
    force_emergency: bool,
    emergency: bool,
    active_crash_ids: set[str],
    active_symbol_upload_ids: set[str],
) -> None:
    """Unlink expired/over-quota staging files while respecting active crash and symbol leases."""
    staging_cutoff_ts = (now - timedelta(hours=storage.settings.STAGING_TTL_HOURS)).timestamp()
    staging_target = int(storage.get_partition_quota_bytes("staging") * threshold_ratio)

    for ts, path, size in candidates:
        rel_parts = path.relative_to(staging_root).parts
        workspace = rel_parts[0]
        if workspace == "uploads" and len(rel_parts) >= 2 and rel_parts[1] in active_crash_ids:
            continue
        if workspace == "symbol_uploads" and len(rel_parts) >= 2:
            uid = rel_parts[1]
            if uid in active_symbol_upload_ids or storage._staging_upload_lease_active(
                staging_root, "symupload", uid
            ):
                continue
        needs_reclaim = force_emergency or (emergency and storage.is_host_floor_breached())
        if ts < staging_cutoff_ts or needs_reclaim or staging_used > staging_target:
            removed = storage._safe_unlink(path)
            if removed > 0 or not path.exists():
                stats.freed_bytes += removed
                stats.deleted_files_count += 1
                staging_used = max(0, staging_used - size)
                if workspace == "symbol_uploads" and len(rel_parts) >= 2:
                    lease_file = staging_root / "symbol_uploads" / rel_parts[1] / ".tmp_lease.json"
                    if lease_file.is_file():
                        # Remove stale lease marker once its payload files are being evicted.
                        storage._safe_unlink(lease_file)
                storage._prune_empty_dirs(path.parent, staging_root)
