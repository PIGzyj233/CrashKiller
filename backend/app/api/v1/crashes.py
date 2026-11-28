"""Crashes REST API, SSE status streaming, artifacts, and human verdict KB feedback."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import (
    get_app_settings,
    get_db,
    get_dispatcher,
    get_effective_storage,
    get_event_bus_dep,
    get_storage,
    require_admin,
    verify_project_upload_auth,
)
from app.config import Settings
from app.db.base import utcnow
from app.db.models import CrashLog, CrashReport
from app.db.session import get_session_factory
from app.schemas.crash import (
    AIAnalysisRunRead,
    AIVerdictLiteral,
    CrashDetailRead,
    CrashListResponse,
    CrashPinRequest,
    CrashPinResponse,
    CrashReprocessRequest,
    CrashReprocessResponse,
    CrashSSEEvent,
    CrashStatusLiteral,
    CrashSummary,
    CrashUploadResponse,
    CrashVerdictRequest,
)
from app.schemas.issue import CrashClusterRead, IssueRead
from app.schemas.log import CrashLogRead
from app.services import clustering
from app.services.clustering import ClusteringError
from app.services.crash_ingest import (
    cleanup_staged_upload_dir,
    stage_crash_upload_files,
)
from app.services.events import (
    TERMINAL_CRASH_STATUSES,
    EventBus,
    normalize_event_payload,
)
from app.services.log_engine import normalize_session_id
from app.services.storage import StorageCapacityError, StorageError, StorageManager
from app.services.symbol_service.symsrv import stream_decompressed_zst_handle
from app.workers.jobs import ACTIVE_CRASH_STATUSES, crash_has_job, stage_crash_job
from app.workers.worker import TaskDispatcher

router = APIRouter(prefix="/crashes", tags=["crashes"])


def _parse_optional_crash_time(raw: str | None) -> datetime:
    if raw is None or not raw.strip():
        return utcnow()
    cleaned = raw.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid ISO-8601 crash_time: {raw!r}",
        ) from exc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _sanitize_log_filename(raw_name: str | None, fallback: str) -> str:
    if not raw_name:
        return fallback
    cleaned = raw_name.replace("\x00", "")
    name = Path(cleaned.replace("\\", "/")).name.strip()
    if name in {"", ".", ".."}:
        return fallback
    if len(name) > 512:
        suffix = Path(name).suffix
        if suffix and len(suffix) < 512:
            stem_part = name[: len(name) - len(suffix)]
            name = stem_part[: 512 - len(suffix)] + suffix
        else:
            name = name[:512]
    return name


def _stream_open_raw_file(fh: BinaryIO, chunk_size: int = 65536) -> Iterator[bytes]:
    try:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        fh.close()


def _measure_upload_stream(stream: BinaryIO) -> int:
    try:
        pos = stream.tell()
        size = stream.seek(0, os.SEEK_END)
        stream.seek(pos)
        return max(0, int(size))
    except (OSError, AttributeError, ValueError):
        return 0


def _serialize_crash_detail(
    crash: CrashReport,
    related_session_crashes: list[CrashReport] | None = None,
    related_session_crash_count: int = 0,
) -> CrashDetailRead:
    sorted_runs = sorted(
        list(crash.ai_runs or []),
        key=lambda r: (r.created_at, str(r.id)),
    )
    sorted_logs = sorted(
        list(crash.logs or []),
        key=lambda lg: (lg.filename, str(lg.id)),
    )
    summary_data = CrashSummary.model_validate(crash).model_dump()
    return CrashDetailRead(
        **summary_data,
        dump_path=crash.dump_path,
        parsed_summary_json=crash.parsed_summary_json,
        parsed_stacks_json=crash.parsed_stacks_json,
        missing_symbols_json=crash.missing_symbols_json,
        logs=[CrashLogRead.model_validate(lg) for lg in sorted_logs],
        cluster=CrashClusterRead.model_validate(crash.cluster) if crash.cluster else None,
        issue=IssueRead.model_validate(crash.issue) if crash.issue else None,
        ai_runs=[AIAnalysisRunRead.model_validate(run) for run in sorted_runs],
        related_session_crashes=[
            CrashSummary.model_validate(item) for item in (related_session_crashes or [])
        ],
        related_session_crash_count=related_session_crash_count,
    )


async def _load_crash_detail(session: AsyncSession, crash_id: uuid.UUID) -> CrashDetailRead:
    stmt = (
        select(CrashReport)
        .options(
            selectinload(CrashReport.logs),
            selectinload(CrashReport.cluster),
            selectinload(CrashReport.issue),
            selectinload(CrashReport.ai_runs),
        )
        .where(CrashReport.id == crash_id)
    )
    crash = (await session.execute(stmt)).scalar_one_or_none()
    if crash is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Crash report not found",
        )

    related_rows: list[CrashReport] = []
    related_count = 0
    if crash.session_id:
        rel_filters = (
            CrashReport.project_id == crash.project_id,
            CrashReport.session_id == crash.session_id,
            CrashReport.id != crash.id,
        )
        related_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(CrashReport).where(*rel_filters)
                )
            ).scalar_one()
        )
        if related_count > 0:
            related_rows = list(
                (
                    await session.execute(
                        select(CrashReport)
                        .where(*rel_filters)
                        .order_by(CrashReport.crash_time.desc(), CrashReport.created_at.desc())
                        .limit(20)
                    )
                )
                .scalars()
                .all()
            )

    return _serialize_crash_detail(
        crash,
        related_session_crashes=related_rows,
        related_session_crash_count=related_count,
    )


def _format_sse_event(payload: dict[str, Any]) -> str:
    event_obj = CrashSSEEvent(
        crash_id=str(payload.get("crash_id") or ""),
        status=str(payload.get("status") or ""),
        stage=payload.get("stage"),
        ai_verdict=payload.get("ai_verdict"),
        error=payload.get("error"),
    )
    return f"event: status\ndata: {json.dumps(event_obj.model_dump())}\n\n"


def _build_terminal_snapshot(crash: CrashReport) -> dict[str, Any]:
    error_msg: str | None = None
    if crash.status == "FAILED":
        error_msg = "Crash analysis failed (corrupt dump or toolchain error)"
    return {
        "crash_id": str(crash.id),
        "status": crash.status,
        "stage": "failed" if crash.status == "FAILED" else "completed",
        "ai_verdict": crash.ai_verdict,
        "error": error_msg,
    }


@router.post("/upload", response_model=CrashUploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_crash(
    project_key: str | None = Form(default=None),
    project_id: str | None = Form(default=None),
    app_version: str = Form(...),
    dump_file: UploadFile = File(...),
    log_files: list[UploadFile] | None = File(default=None),
    build_id: str | None = Form(default=None),
    os_version: str | None = Form(default=None),
    session_id: str | None = Form(default=None),
    crash_time: str | None = Form(default=None),
    api_key: str | None = Form(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
    base_settings: Settings = Depends(get_app_settings),
    dispatcher: TaskDispatcher = Depends(get_dispatcher),
) -> CrashUploadResponse:
    project = await verify_project_upload_auth(
        session=session,
        project_key=project_key,
        project_id=project_id,
        header_api_key=x_api_key,
        form_api_key=api_key,
    )

    cleaned_version = app_version.strip()
    if not cleaned_version:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="app_version must not be empty",
        )
    if len(cleaned_version) > 128:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="app_version must not exceed 128 characters",
        )

    cleaned_build_id = build_id.strip() if build_id and build_id.strip() else None
    if cleaned_build_id and len(cleaned_build_id) > 128:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="build_id must not exceed 128 characters",
        )

    cleaned_os_version = os_version.strip() if os_version and os_version.strip() else None
    if cleaned_os_version and len(cleaned_os_version) > 255:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="os_version must not exceed 255 characters",
        )

    try:
        cleaned_session_id = normalize_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    parsed_crash_time = _parse_optional_crash_time(crash_time)
    effective_app, eff_storage = await get_effective_storage(session, storage, base_settings)

    crash_id = uuid.uuid4()
    upload_headroom = {"dumps": 1}
    if log_files:
        upload_headroom["logs"] = 1

    log_inputs: list[tuple[uuid.UUID, str, Any]] = []
    for lf in log_files or []:
        lid = uuid.uuid4()
        safe_name = _sanitize_log_filename(lf.filename, f"{lid}.log")
        log_inputs.append((lid, safe_name, lf.file))

    reserved_bytes = _measure_upload_stream(dump_file.file) + sum(
        _measure_upload_stream(stream) for _, _, stream in log_inputs
    )
    reserved_files = 1 + len(log_inputs)
    reservation_committed = False

    async with eff_storage.storage_io_lock(exclusive=False):
        async with eff_storage.async_staging_upload_lease("upload", str(crash_id)):
            try:
                await eff_storage.commit_usage_deltas(
                    session,
                    {"staging": max(1, reserved_bytes) if reserved_bytes > 0 else 0},
                    {"staging": reserved_files},
                    enforce_quota=True,
                    extra_headroom_checks=upload_headroom,
                )
                await session.commit()
                reservation_committed = True

                try:
                    staged = await asyncio.to_thread(
                        stage_crash_upload_files,
                        eff_storage,
                        crash_id,
                        dump_file.file,
                        log_inputs,
                    )
                except OSError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                        detail=f"Failed to stage crash upload: {exc}",
                    ) from exc
                except StorageError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=str(exc),
                    ) from exc

                now = utcnow()
                crash = CrashReport(
                    id=crash_id,
                    project_id=project.id,
                    status="PENDING",
                    app_version=cleaned_version,
                    build_id=cleaned_build_id,
                    os_version=cleaned_os_version,
                    session_id=cleaned_session_id,
                    dump_path=staged.dump_rel_path,
                    dump_raw_size=staged.dump_raw_size,
                    dump_compressed_size=0,
                    crash_time=parsed_crash_time,
                    created_at=now,
                )
                session.add(crash)

                log_ids: list[uuid.UUID] = []
                for item in staged.logs:
                    session.add(
                        CrashLog(
                            id=item.log_id,
                            crash_id=crash_id,
                            filename=item.filename,
                            zst_path=None,
                            idx_path=None,
                            staged_raw_path=item.rel_raw_path,
                            raw_size=item.raw_size,
                            compressed_size=0,
                            total_lines=0,
                            digest_json={},
                        )
                    )
                    log_ids.append(item.log_id)

                await session.flush()
                job_id = stage_crash_job(session, crash_id, "parse_crash")
                await session.flush()

                byte_diff = staged.total_staged_bytes - reserved_bytes
                file_diff = staged.total_staged_files - reserved_files
                if byte_diff != 0 or file_diff != 0:
                    await eff_storage.commit_usage_deltas(
                        session,
                        {"staging": byte_diff},
                        {"staging": file_diff},
                        enforce_quota=byte_diff > 0,
                        extra_headroom_checks=upload_headroom if byte_diff > 0 else None,
                    )
                await session.commit()
            except BaseException as exc:
                cleanup_staged_upload_dir(eff_storage, crash_id)
                try:
                    await session.rollback()
                except Exception:
                    pass
                if reservation_committed:
                    try:
                        await eff_storage.commit_usage_deltas(
                            session,
                            {"staging": -reserved_bytes},
                            {"staging": -reserved_files},
                            enforce_quota=False,
                        )
                        await session.commit()
                    except Exception:
                        try:
                            await session.rollback()
                        except Exception:
                            pass
                if isinstance(exc, StorageCapacityError):
                    raise HTTPException(
                        status_code=status.HTTP_507_INSUFFICIENT_STORAGE, detail=str(exc)
                    ) from exc
                raise

    await dispatcher.kick_crash_jobs([job_id], task_by_job_id={job_id: "parse_crash"})

    return CrashUploadResponse(
        crash_id=crash_id,
        status="PENDING",
        log_ids=log_ids,
    )


@router.get("", response_model=CrashListResponse)
async def list_crashes(
    project_id: uuid.UUID | None = None,
    status_filter: CrashStatusLiteral | None = Query(default=None, alias="status"),
    ai_verdict: AIVerdictLiteral | None = None,
    issue_id: int | None = None,
    cluster_id: uuid.UUID | None = None,
    app_version: str | None = None,
    session_id: str | None = None,
    is_pinned: bool | None = None,
    is_human_verified: bool | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> CrashListResponse:
    filters = []
    if project_id is not None:
        filters.append(CrashReport.project_id == project_id)
    if status_filter is not None:
        filters.append(CrashReport.status == status_filter)
    if ai_verdict is not None:
        filters.append(CrashReport.ai_verdict == ai_verdict)
    if issue_id is not None:
        filters.append(CrashReport.issue_id == issue_id)
    if cluster_id is not None:
        filters.append(CrashReport.cluster_id == cluster_id)
    if app_version is not None and app_version.strip():
        filters.append(CrashReport.app_version == app_version.strip())
    if session_id is not None and session_id.strip():
        filters.append(CrashReport.session_id == session_id.strip())
    if is_pinned is not None:
        filters.append(CrashReport.is_pinned == is_pinned)
    if is_human_verified is not None:
        filters.append(CrashReport.is_human_verified == is_human_verified)
    if q is not None and q.strip():
        pattern = f"%{q.strip()}%"
        filters.append(
            or_(
                CrashReport.crash_reason.ilike(pattern),
                CrashReport.crash_address.ilike(pattern),
                CrashReport.app_version.ilike(pattern),
                CrashReport.build_id.ilike(pattern),
                CrashReport.os_version.ilike(pattern),
                CrashReport.session_id.ilike(pattern),
            )
        )

    count_stmt = select(func.count()).select_from(CrashReport)
    list_stmt = select(CrashReport)
    if filters:
        count_stmt = count_stmt.where(*filters)
        list_stmt = list_stmt.where(*filters)

    total = int((await session.execute(count_stmt)).scalar_one())
    rows = (
        await session.execute(
            list_stmt.order_by(CrashReport.created_at.desc()).offset(offset).limit(limit)
        )
    ).scalars().all()
    return CrashListResponse(
        items=[CrashSummary.model_validate(row) for row in rows],
        total=total,
    )


@router.get("/{crash_id}", response_model=CrashDetailRead)
async def get_crash(
    crash_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> CrashDetailRead:
    return await _load_crash_detail(session, crash_id)


async def _read_crash_snapshot(
    request: Request,
    crash_id: uuid.UUID,
    api_key: str = "",
) -> dict[str, Any] | None:
    session_factory = (
        getattr(request.app.state, "session_factory", None) or get_session_factory()
    )
    async with session_factory() as session:
        crash = await session.get(CrashReport, crash_id)
        if crash is None:
            return None
        if api_key:
            await verify_project_upload_auth(
                session=session,
                project_key=None,
                project_id=str(crash.project_id),
                header_api_key=api_key,
                form_api_key=None,
            )
        if crash.status in TERMINAL_CRASH_STATUSES:
            return _build_terminal_snapshot(crash)
        return {
            "crash_id": str(crash.id),
            "status": crash.status,
            "stage": "current",
            "ai_verdict": crash.ai_verdict,
            "error": None,
        }


async def _authorize_crash_status(
    request: Request, crash_id: uuid.UUID, x_api_key: str | None
) -> dict[str, Any]:
    api_key = (x_api_key or "").strip()
    if not api_key:
        await require_admin(request)
    snapshot = await _read_crash_snapshot(request, crash_id, api_key)
    if snapshot is None:
        if api_key:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API key")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Crash report not found",
        )
    return snapshot


@router.get("/{crash_id}/status", response_model_exclude={"stage"})
async def get_crash_status(
    crash_id: uuid.UUID,
    request: Request,
    response: Response,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> CrashSSEEvent:
    snapshot = await _authorize_crash_status(request, crash_id, x_api_key)
    response.headers["Cache-Control"] = "no-store"
    return CrashSSEEvent.model_validate(snapshot)


@router.get("/{crash_id}/events")
async def stream_crash_events(
    crash_id: uuid.UUID,
    request: Request,
    event_bus: EventBus = Depends(get_event_bus_dep),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> StreamingResponse:
    await _authorize_crash_status(request, crash_id, x_api_key)

    async def event_generator():
        async with event_bus.subscribe(crash_id) as sub:
            initial_payload = await _read_crash_snapshot(request, crash_id)
            if initial_payload is None:
                return
            yield _format_sse_event(initial_payload)
            if initial_payload["status"] in TERMINAL_CRASH_STATUSES:
                return

            keepalive_ticks = 0
            while True:
                try:
                    raw_event = await sub.get(timeout=15.0)
                except TimeoutError:
                    keepalive_ticks += 1
                    if keepalive_ticks % 4 == 0:
                        current = await _read_crash_snapshot(request, crash_id)
                        if current is not None and current["status"] in TERMINAL_CRASH_STATUSES:
                            yield _format_sse_event(current)
                            return
                    yield ": keepalive\n\n"
                    continue
                except StopAsyncIteration:
                    return

                keepalive_ticks = 0

                normalized = normalize_event_payload(crash_id, raw_event)
                if normalized.get("status") == "FAILED" and not normalized.get("error"):
                    normalized["error"] = "Crash analysis failed"
                yield _format_sse_event(normalized)
                if normalized.get("status") in TERMINAL_CRASH_STATUSES:
                    return

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/{crash_id}/reprocess",
    response_model=CrashReprocessResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reprocess_crash(
    crash_id: uuid.UUID,
    payload: CrashReprocessRequest,
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
    dispatcher: TaskDispatcher = Depends(get_dispatcher),
) -> CrashReprocessResponse:
    crash = await session.get(CrashReport, crash_id)
    if crash is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Crash report not found",
        )

    if crash.status in ACTIVE_CRASH_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Crash report is currently in-flight ({crash.status}) "
                "and cannot be reprocessed"
            ),
        )

    if payload.mode == "full":
        dump_ok = False
        if crash.dump_path:
            try:
                dump_ok = storage.resolve_path(crash.dump_path).is_file()
            except StorageError:
                dump_ok = False
        if not dump_ok:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Crash dump has been evicted or is unavailable for full reprocess",
            )
    elif crash.parsed_stacks_json is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Crash report has no parsed stack trace for triage reprocess",
        )

    target_status: CrashStatusLiteral = "PENDING" if payload.mode == "full" else "TRIAGING"
    conditions = [
        CrashReport.id == crash_id,
        CrashReport.status.notin_(ACTIVE_CRASH_STATUSES),
        ~crash_has_job(),
    ]
    if payload.mode == "full":
        conditions.append(CrashReport.dump_path.is_not(None))
    upd = await session.execute(
        update(CrashReport).where(*conditions).values(status=target_status)
    )
    if (upd.rowcount or 0) == 0:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Crash report is currently in-flight or evicted and cannot be reprocessed",
        )
    if payload.mode == "full":
        reproc_task = "parse_crash"
        job_id = stage_crash_job(session, crash_id, "parse_crash", run_ai_override=True)
    else:
        reproc_task = "ai_triage"
        job_id = stage_crash_job(session, crash_id, "ai_triage")
    await session.commit()
    await dispatcher.kick_crash_jobs([job_id], task_by_job_id={job_id: reproc_task})
    return CrashReprocessResponse(crash_id=crash_id, status=target_status)


@router.post("/{crash_id}/verdict", response_model=CrashDetailRead)
async def submit_crash_verdict(
    crash_id: uuid.UUID,
    payload: CrashVerdictRequest,
    session: AsyncSession = Depends(get_db),
) -> CrashDetailRead:
    crash = await session.get(CrashReport, crash_id)
    if crash is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Crash report not found",
        )

    new_issue_data: dict[str, Any] | None = (
        payload.new_issue.model_dump() if payload.new_issue is not None else None
    )
    resolved_root_cause = payload.root_cause
    if resolved_root_cause is None and new_issue_data is not None:
        resolved_root_cause = new_issue_data.get("root_cause")

    try:
        await clustering.apply_human_verdict(
            session=session,
            crash_report=crash,
            action=payload.action,
            issue_id=payload.issue_id,
            new_issue_data=new_issue_data,
            root_cause=resolved_root_cause,
            add_log_patterns=payload.add_log_patterns,
            is_ambiguous_stack=payload.is_ambiguous_stack,
            commit=True,
        )
    except ClusteringError as exc:
        msg = str(exc)
        status_code = (
            status.HTTP_404_NOT_FOUND
            if "not found" in msg.lower()
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=status_code, detail=msg) from exc

    return await _load_crash_detail(session, crash_id)


@router.patch("/{crash_id}/pin", response_model=CrashPinResponse)
async def pin_crash(
    crash_id: uuid.UUID,
    payload: CrashPinRequest,
    session: AsyncSession = Depends(get_db),
) -> CrashPinResponse:
    crash = await session.get(CrashReport, crash_id)
    if crash is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Crash report not found",
        )
    crash.is_pinned = payload.is_pinned
    await session.commit()
    return CrashPinResponse(crash_id=crash.id, is_pinned=crash.is_pinned)


@router.get("/{crash_id}/artifacts/{kind}")
async def download_crash_artifact(
    crash_id: uuid.UUID,
    kind: str,
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
) -> Any:
    if kind not in {"dump", "stacks_json"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unsupported artifact kind: {kind!r}",
        )

    crash = await session.get(CrashReport, crash_id)
    if crash is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Crash report not found",
        )

    if kind == "dump":
        if not crash.dump_path:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Crash dump artifact is unavailable or evicted by GC",
            )

        def _try_open_dump(rel_path: str) -> BinaryIO | None:
            try:
                abs_path = storage.resolve_path(rel_path)
            except StorageError as exc:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Crash dump artifact path invalid",
                ) from exc
            try:
                return abs_path.open("rb")
            except (FileNotFoundError, IsADirectoryError):
                return None

        active_dump_path = crash.dump_path
        dump_fh = _try_open_dump(active_dump_path)
        if dump_fh is None:
            # Refresh once in case worker just finalized staged dump -> dumps/<id>.dmp.zst.
            await session.refresh(crash, attribute_names=["dump_path"])
            if not crash.dump_path:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Crash dump artifact is unavailable or evicted by GC",
                )
            active_dump_path = crash.dump_path
            dump_fh = _try_open_dump(active_dump_path)
            if dump_fh is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Crash dump artifact file missing on disk",
                )

        try:
            stream_iter = (
                stream_decompressed_zst_handle(dump_fh)
                if active_dump_path.endswith(".zst")
                else _stream_open_raw_file(dump_fh)
            )
            return StreamingResponse(
                stream_iter,
                status_code=status.HTTP_200_OK,
                media_type="application/octet-stream",
                headers={
                    "Content-Disposition": f'attachment; filename="{crash_id}.dmp"',
                },
            )
        except BaseException:
            dump_fh.close()
            raise

    if crash.parsed_stacks_json is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Parsed stacks JSON is not available",
        )
    return JSONResponse(
        content=crash.parsed_stacks_json,
        status_code=status.HTTP_200_OK,
        headers={
            "Content-Disposition": f'attachment; filename="{crash_id}_stacks.json"',
        },
    )
