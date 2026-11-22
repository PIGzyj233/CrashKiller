"""Durable crash work records and claims shared by the API and worker."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from arq.connections import ArqRedis
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Exists

from app.db.base import utcnow
from app.db.models import CrashJob, CrashReport

CRASH_JOB_TIMEOUT_SECONDS = 600
JOB_DELIVERY_TIMEOUT_SECONDS = 5
RECOVERY_BATCH_SIZE = 100
ACTIVE_CRASH_STATUSES = ("PENDING", "PARSING", "TRIAGING")
CrashJobTask = Literal["parse_crash", "ai_triage", "resymbolicate"]


@dataclass(frozen=True, slots=True)
class CrashJobClaim:
    job_id: uuid.UUID
    lease_id: uuid.UUID
    crash_id: uuid.UUID
    task: str
    run_ai_override: bool | None


class CrashJobSuperseded(Exception):
    """A different worker attempt owns this job generation."""


def stage_crash_job(
    session: AsyncSession,
    crash_id: uuid.UUID,
    task: CrashJobTask,
    *,
    run_ai_override: bool | None = None,
) -> uuid.UUID:
    job_id = uuid.uuid4()
    session.add(CrashJob(
        id=job_id, crash_id=crash_id, task=task, run_ai_override=run_ai_override,
    ))
    return job_id


def crash_has_job() -> Exists:
    return select(CrashJob.id).where(CrashJob.crash_id == CrashReport.id).exists()


def _claimable() -> ColumnElement[bool]:
    return or_(CrashJob.lease_expires_at.is_(None), CrashJob.lease_expires_at <= utcnow())


async def pending_crash_jobs(session: AsyncSession) -> list[uuid.UUID]:
    result = await session.scalars(
        select(CrashJob.id).where(_claimable())
        .order_by(CrashJob.created_at, CrashJob.id).limit(RECOVERY_BATCH_SIZE)
    )
    return list(result)


async def claim_crash_job(session: AsyncSession, job_id: uuid.UUID) -> CrashJobClaim | None:
    lease_id = uuid.uuid4()
    result = await session.execute(
        update(CrashJob).where(CrashJob.id == job_id, _claimable()).values(
            lease_id=lease_id,
            lease_expires_at=utcnow() + timedelta(seconds=CRASH_JOB_TIMEOUT_SECONDS + 30),
        ).returning(CrashJob.crash_id, CrashJob.task, CrashJob.run_ai_override)
    )
    row = result.one_or_none()
    await session.commit()
    return CrashJobClaim(job_id, lease_id, *row) if row is not None else None


async def finish_crash_job(session: AsyncSession, claim: CrashJobClaim) -> None:
    result = await session.execute(delete(CrashJob).where(
        CrashJob.id == claim.job_id, CrashJob.lease_id == claim.lease_id,
    ))
    if result.rowcount != 1:
        raise CrashJobSuperseded(str(claim.job_id))


async def lock_current_crash_job(session: AsyncSession, claim: CrashJobClaim) -> None:
    current = await session.scalar(
        select(CrashJob.id)
        .where(CrashJob.id == claim.job_id, CrashJob.lease_id == claim.lease_id)
        .with_for_update()
    )
    if current is None:
        raise CrashJobSuperseded(str(claim.job_id))


async def release_crash_job(session: AsyncSession, claim: CrashJobClaim) -> None:
    await session.execute(
        update(CrashJob).where(CrashJob.id == claim.job_id, CrashJob.lease_id == claim.lease_id)
        .values(lease_id=None, lease_expires_at=None)
    )
    await session.commit()


async def enqueue_crash_job(redis_pool: ArqRedis, job_id: uuid.UUID) -> None:
    await redis_pool.enqueue_job("run_crash_job", str(job_id), _job_id=f"crash-job:{job_id}")
