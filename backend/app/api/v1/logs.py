"""Logs REST API endpoints (/api/v1/logs)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_storage
from app.db.models import CrashLog, CrashReport
from app.schemas.log import (
    LogAroundTimeResponse,
    LogLine,
    LogLinesResponse,
    LogSearchMatch,
    LogSearchResponse,
    LogSessionLinesResponse,
    LogSummaryResponse,
)
from app.services.log_engine import LogEngine, _parse_ts, normalize_session_id
from app.services.storage import StorageError, StorageManager

router = APIRouter(prefix="/logs", tags=["logs"])


def _resolve_log_paths(
    log_row: CrashLog,
    storage: StorageManager,
) -> tuple[Path, Path]:
    if not log_row.zst_path or not log_row.idx_path:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Compressed log artifact has been evicted by GC",
        )
    try:
        zst_abs = storage.resolve_path(log_row.zst_path)
        idx_abs = storage.resolve_path(log_row.idx_path)
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Compressed log artifact is unavailable",
        ) from exc
    if not zst_abs.is_file() or not idx_abs.is_file():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Compressed log files are missing on disk",
        )
    return zst_abs, idx_abs


async def _get_log_or_404(session: AsyncSession, log_id: uuid.UUID) -> CrashLog:
    log_row = await session.get(CrashLog, log_id)
    if log_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Crash log not found",
        )
    return log_row


async def _resolve_session_filter(
    session: AsyncSession,
    log_row: CrashLog,
    session_id: str | None,
    only_session: bool,
    *,
    require_session: bool = False,
) -> str | None:
    try:
        cleaned = normalize_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    if cleaned is not None:
        return cleaned
    if only_session or require_session:
        crash = await session.get(CrashReport, log_row.crash_id)
        if crash is not None and crash.session_id:
            return crash.session_id
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="This crash report has no session_id to filter by",
        )
    return None


@router.get("/{log_id}/summary", response_model=LogSummaryResponse)
async def get_log_summary(
    log_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> LogSummaryResponse:
    log_row = await _get_log_or_404(session, log_id)
    return LogSummaryResponse(
        id=log_row.id,
        crash_id=log_row.crash_id,
        filename=log_row.filename,
        raw_size=log_row.raw_size,
        compressed_size=log_row.compressed_size,
        total_lines=log_row.total_lines,
        digest=dict(log_row.digest_json or {}),
    )


@router.get("/{log_id}/lines", response_model=LogLinesResponse)
async def get_log_lines(
    log_id: uuid.UUID,
    start: int = Query(default=1, ge=1),
    end: int = Query(default=200, ge=1),
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
) -> LogLinesResponse:
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start must be less than or equal to end",
        )

    log_row = await _get_log_or_404(session, log_id)
    zst_abs, idx_abs = _resolve_log_paths(log_row, storage)

    engine = LogEngine()
    raw_lines = await asyncio.to_thread(engine.read_lines, zst_abs, idx_abs, start, end)
    lines = [LogLine(**item) for item in raw_lines]

    total = int(log_row.total_lines or 0)
    if lines:
        resp_start = lines[0].line_number
        resp_end = lines[-1].line_number
    else:
        resp_start = 0
        resp_end = 0

    return LogLinesResponse(
        log_id=log_row.id,
        total_lines=total,
        start_line=resp_start,
        end_line=resp_end,
        lines=lines,
    )


@router.get("/{log_id}/session-lines", response_model=LogSessionLinesResponse)
async def get_log_session_lines(
    log_id: uuid.UUID,
    session_id: str | None = Query(default=None),
    start: int = Query(default=1, ge=1),
    end: int = Query(default=200, ge=1),
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
) -> LogSessionLinesResponse:
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start must be less than or equal to end",
        )
    if end - start + 1 > 1000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Requested session line range must not exceed 1000 lines",
        )

    log_row = await _get_log_or_404(session, log_id)
    effective_session_id = await _resolve_session_filter(
        session,
        log_row,
        session_id,
        True,
        require_session=True,
    )
    assert effective_session_id is not None
    zst_abs, idx_abs = _resolve_log_paths(log_row, storage)

    engine = LogEngine()
    try:
        res = await asyncio.to_thread(
            engine.read_session_lines,
            zst_abs,
            idx_abs,
            effective_session_id,
            start,
            end,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    lines = [LogLine(**item) for item in res.get("lines", [])]
    return LogSessionLinesResponse(
        log_id=log_row.id,
        session_id=str(res["session_id"]),
        total_matches=int(res["total_matches"]),
        start_index=int(res["start_index"]),
        end_index=int(res["end_index"]),
        lines=lines,
    )


@router.get("/{log_id}/around-time", response_model=LogAroundTimeResponse)
async def get_log_around_time(
    log_id: uuid.UUID,
    timestamp: str = Query(..., min_length=1),
    window_sec: int = Query(default=10, ge=0, le=86400),
    max_lines: int = Query(default=500, ge=1, le=5000),
    session_id: str | None = Query(default=None),
    only_session: bool = Query(default=False),
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
) -> LogAroundTimeResponse:
    parsed_dt = _parse_ts(timestamp.strip())
    if parsed_dt is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid timestamp format: {timestamp!r}",
        )
    center_iso = parsed_dt.astimezone(UTC).isoformat().replace("+00:00", "Z")

    log_row = await _get_log_or_404(session, log_id)
    effective_session_id = await _resolve_session_filter(
        session, log_row, session_id, only_session
    )
    zst_abs, idx_abs = _resolve_log_paths(log_row, storage)

    engine = LogEngine()
    try:
        raw_lines = await asyncio.to_thread(
            engine.read_around_time,
            zst_abs,
            idx_abs,
            parsed_dt,
            window_sec,
            max_lines,
            effective_session_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    lines = [LogLine(**item) for item in raw_lines]
    start_line = lines[0].line_number if lines else 0
    end_line = lines[-1].line_number if lines else 0

    return LogAroundTimeResponse(
        log_id=log_row.id,
        center_timestamp=center_iso,
        window_sec=window_sec,
        start_line=start_line,
        end_line=end_line,
        session_id=effective_session_id,
        lines=lines,
    )


@router.get("/{log_id}/search", response_model=LogSearchResponse)
async def search_log(
    log_id: uuid.UUID,
    pattern: str = Query(..., min_length=1),
    is_regex: bool = Query(default=False),
    max_matches: int = Query(default=100, ge=1, le=1000),
    case_sensitive: bool = Query(default=False),
    session_id: str | None = Query(default=None),
    only_session: bool = Query(default=False),
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
) -> LogSearchResponse:
    log_row = await _get_log_or_404(session, log_id)
    effective_session_id = await _resolve_session_filter(
        session, log_row, session_id, only_session
    )
    zst_abs, idx_abs = _resolve_log_paths(log_row, storage)

    engine = LogEngine()
    try:
        result = await asyncio.to_thread(
            engine.search,
            zst_abs,
            idx_abs,
            pattern,
            is_regex,
            max_matches,
            case_sensitive,
            effective_session_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    matches = [LogSearchMatch(**m) for m in result.get("matches", [])]
    return LogSearchResponse(
        log_id=log_row.id,
        pattern=pattern,
        total_matches=int(result["total_matches"]),
        truncated=bool(result["truncated"]),
        elapsed_ms=float(result["elapsed_ms"]),
        session_id=effective_session_id,
        matches=matches,
    )
