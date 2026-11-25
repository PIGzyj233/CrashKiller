"""Batch crash resymbolication status and enqueue endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_dispatcher, get_storage
from app.api.v1.symbols.helpers import _queue_resymbolication_with_stats
from app.db.models import CrashReport
from app.schemas.symbol import (
    ResymbolicateRequest,
    ResymbolicateResponse,
    ResymbolicateStatusResponse,
)
from app.services.storage import StorageManager
from app.workers.worker import TaskDispatcher

resymbolicate_router = APIRouter()


@resymbolicate_router.get("/resymbolicate/status", response_model=ResymbolicateStatusResponse)
async def get_resymbolicate_status(
    response: Response,
    crash_ids: list[str] = Query(..., min_length=1),
    session: AsyncSession = Depends(get_db),
) -> ResymbolicateStatusResponse:
    """Return batch symbolication progress for a set of queued crash IDs."""
    parsed_ids: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for raw in crash_ids:
        for part in raw.split(","):
            token = part.strip()
            if not token:
                continue
            try:
                cid = uuid.UUID(token)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Invalid crash_id UUID: {token!r}",
                ) from exc
            if cid not in seen:
                seen.add(cid)
                parsed_ids.append(cid)
    if not parsed_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="At least one crash_id is required",
        )
    if len(parsed_ids) > 200:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="At most 200 crash_ids can be queried at once",
        )

    rows = (
        await session.execute(
            select(CrashReport.status).where(CrashReport.id.in_(parsed_ids))
        )
    ).scalars().all()

    pending = 0
    parsing = 0
    triaging = 0
    analyzed = 0
    failed = 0
    for st in rows:
        if st == "PENDING":
            pending += 1
        elif st == "PARSING":
            parsing += 1
        elif st == "TRIAGING":
            triaging += 1
        elif st == "ANALYZED":
            analyzed += 1
        elif st == "FAILED":
            failed += 1

    total = len(parsed_ids)
    in_progress = pending + parsing
    completed = max(0, total - in_progress)
    response.headers["Cache-Control"] = "no-store"
    return ResymbolicateStatusResponse(
        total=total,
        pending=pending,
        parsing=parsing,
        triaging=triaging,
        analyzed=analyzed,
        failed=failed,
        completed=completed,
        in_progress=in_progress,
        done=in_progress == 0,
    )


@resymbolicate_router.post(
    "/resymbolicate",
    response_model=ResymbolicateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resymbolicate_crashes(
    payload: ResymbolicateRequest,
    session: AsyncSession = Depends(get_db),
    dispatcher: TaskDispatcher = Depends(get_dispatcher),
    storage: StorageManager = Depends(get_storage),
) -> ResymbolicateResponse:
    queued_ids, active_ids = await _queue_resymbolication_with_stats(
        session=session,
        dispatcher=dispatcher,
        storage=storage,
        project_id=payload.project_id,
        debug_ids={payload.debug_id} if payload.debug_id else None,
        crash_ids=payload.crash_ids,
        missing_only=payload.missing_only,
    )
    return ResymbolicateResponse(
        queued_count=len(queued_ids),
        crash_ids=queued_ids,
        skipped_active_count=len(active_ids),
        active_crash_ids=active_ids,
    )
