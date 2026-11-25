"""Symbols REST API (/api/v1/symbols) and WinDbg SymSrv (/symsrv) endpoints."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

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
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_app_settings,
    get_db,
    get_dispatcher,
    get_effective_storage,
    get_storage,
    verify_project_upload_auth,
)
from app.config import Settings
from app.db.models import CrashReport, Symbol
from app.db.session import get_session_factory
from app.schemas.symbol import (
    MissingSymbolQueueItem,
    PublicSymbolSyncItemResult,
    PublicSymbolSyncRequest,
    PublicSymbolSyncResponse,
    ResymbolicateRequest,
    ResymbolicateResponse,
    ResymbolicateStatusResponse,
    SymbolListResponse,
    SymbolRead,
    SymbolUploadResponse,
)
from app.services import ms_symbol_service, symbol_service
from app.services.storage import StorageCapacityError, StorageError, StorageManager
from app.services.symbol_service import (
    SymbolConversionError,
    resolve_symsrv_file,
    stream_decompressed_zst,
)
from app.workers.jobs import ACTIVE_CRASH_STATUSES, crash_has_job, stage_crash_job
from app.workers.worker import TaskDispatcher

router = APIRouter(prefix="/symbols", tags=["symbols"])
symsrv_router = APIRouter(prefix="/symsrv", tags=["symsrv"])

_ALL_ZERO_DEBUG_ID_RE = re.compile(r"^0+$")
ACTIVE_MS_SYNC_CONCURRENCY = 4
MAX_EXPLICIT_SYNC_TARGETS = 500
DEFAULT_MISSING_SYNC_CEILING = 2000


def _sanitize_upload_basename(filename: str | None, fallback: str) -> str:
    if not filename:
        return fallback
    name = Path(filename.replace("\\", "/")).name.strip()
    if not name:
        return fallback
    if name in {".", ".."} or "\x00" in name or ".." in name:
        return name
    return symbol_service.clamp_upload_filename(name, max_bytes=240)


def _as_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.fromtimestamp(0, tz=UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


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


async def aggregate_missing_symbols(
    session: AsyncSession,
    project_id: uuid.UUID | None = None,
    limit: int | None = 50,
    offset: int = 0,
    storage: StorageManager | None = None,
) -> tuple[list[MissingSymbolQueueItem], int]:
    """Aggregate missing symbols from CrashReport.missing_symbols_json across crashes."""
    stmt = select(
        CrashReport.id,
        CrashReport.project_id,
        CrashReport.missing_symbols_json,
        CrashReport.crash_time,
        CrashReport.created_at,
    ).where(CrashReport.missing_symbols_json.is_not(None))
    if project_id is not None:
        stmt = stmt.where(CrashReport.project_id == project_id)

    rows = (await session.execute(stmt)).all()
    buckets: dict[tuple[str, str], dict[str, Any]] = {}

    for crash_id, crash_project_id, missing_list, crash_time, created_at in rows:
        if not isinstance(missing_list, list) or not missing_list:
            continue
        event_ts = _as_utc(crash_time or created_at)
        seen_in_crash: set[tuple[str, str]] = set()
        for entry in missing_list:
            if not isinstance(entry, dict):
                continue
            raw_df = entry.get("debug_file")
            raw_did = entry.get("debug_id")
            raw_df_str = str(raw_df).strip() if raw_df is not None else ""
            debug_file = (
                ms_symbol_service.normalize_debug_file_name(raw_df_str) if raw_df_str else ""
            )
            debug_id = str(raw_did).strip().upper() if raw_did is not None else ""
            if not debug_file and (not debug_id or _ALL_ZERO_DEBUG_ID_RE.fullmatch(debug_id)):
                continue

            key = (debug_file.lower(), debug_id)
            if key in seen_in_crash:
                continue
            seen_in_crash.add(key)

            raw_mod = entry.get("module_name")
            module_name = (
                str(raw_mod).strip()
                if raw_mod is not None and str(raw_mod).strip()
                else (debug_file or "unknown")
            )
            code_file = (
                str(entry["code_file"]).strip()
                if entry.get("code_file") is not None and str(entry["code_file"]).strip()
                else None
            )
            code_id = (
                str(entry["code_id"]).strip().upper()
                if entry.get("code_id") is not None and str(entry["code_id"]).strip()
                else None
            )

            bucket = buckets.get(key)
            if bucket is None:
                buckets[key] = {
                    "debug_file": debug_file,
                    "debug_id": debug_id,
                    "module_name": module_name,
                    "code_file": code_file,
                    "code_id": code_id,
                    "latest_crash_at": event_ts,
                    "affected_crash_ids": [crash_id],
                    "project_ids": {crash_project_id} if crash_project_id is not None else set(),
                }
            else:
                bucket["affected_crash_ids"].append(crash_id)
                if crash_project_id is not None:
                    bucket["project_ids"].add(crash_project_id)
                if event_ts > bucket["latest_crash_at"]:
                    bucket["latest_crash_at"] = event_ts
                if not bucket["code_file"] and code_file:
                    bucket["code_file"] = code_file
                if not bucket["code_id"] and code_id:
                    bucket["code_id"] = code_id
                if bucket["module_name"] == "unknown" and module_name != "unknown":
                    bucket["module_name"] = module_name

    raw_buckets = list(buckets.values())
    raw_buckets.sort(
        key=lambda b: (
            -len(b["affected_crash_ids"]),
            -b["latest_crash_at"].timestamp(),
            b["debug_file"].lower(),
            b["debug_id"],
        )
    )
    total = len(raw_buckets)
    page_buckets = (
        raw_buckets[offset : offset + limit] if limit is not None else raw_buckets[offset:]
    )

    items: list[MissingSymbolQueueItem] = []
    for b in page_buckets:
        debug_file = b["debug_file"]
        debug_id = b["debug_id"]
        is_win_sys = ms_symbol_service.is_windows_system_module(
            filename=b["code_file"],
            code_file=b["code_file"],
            module_name=b["module_name"],
            debug_file=debug_file,
        )
        is_avail = False
        loc_scope: Literal["public", "project"] | None = None
        neg_cached = False
        neg_reason: str | None = None
        if storage is not None and debug_file and debug_id:
            if project_id is not None:
                is_avail, raw_scope, _ = ms_symbol_service.has_cached_symbol_detail(
                    storage,
                    debug_file,
                    debug_id,
                    project_id=project_id,
                )
            else:
                is_avail, raw_scope, _ = ms_symbol_service.has_cached_symbol_detail(
                    storage,
                    debug_file,
                    debug_id,
                    project_id=None,
                )
                affected_projects: set[uuid.UUID] = b["project_ids"]
                if not is_avail and affected_projects:
                    if all(
                        ms_symbol_service.has_cached_symbol_detail(
                            storage, debug_file, debug_id, project_id=pid
                        )[0]
                        for pid in affected_projects
                    ):
                        is_avail = True
                        raw_scope = "project"
            if raw_scope in {"public", "project"}:
                loc_scope = raw_scope  # type: ignore[assignment]
            neg_info = ms_symbol_service.get_negative_cache_info(storage, debug_file, debug_id)
            if neg_info is not None:
                neg_cached = True
                raw_reason = neg_info.get("reason")
                neg_reason = str(raw_reason) if raw_reason else None

        items.append(
            MissingSymbolQueueItem(
                debug_file=debug_file,
                debug_id=debug_id,
                module_name=b["module_name"],
                code_file=b["code_file"],
                code_id=b["code_id"],
                crash_count=len(b["affected_crash_ids"]),
                latest_crash_at=b["latest_crash_at"],
                affected_crash_ids=b["affected_crash_ids"],
                is_windows_system=is_win_sys,
                is_available_locally=is_avail,
                local_scope=loc_scope,
                msdl_negative_cached=neg_cached,
                msdl_last_reason=neg_reason,
            )
        )

    return items, total


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


@router.post("/upload", response_model=SymbolUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_symbol(
    request: Request,
    project_key: str | None = Form(default=None),
    project_id: str | None = Form(default=None),
    app_version: str = Form(...),
    pdb_file: UploadFile = File(...),
    pe_file: UploadFile | None = File(default=None),
    api_key: str | None = Form(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
    base_settings: Settings = Depends(get_app_settings),
) -> SymbolUploadResponse:
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

    pdb_name = _sanitize_upload_basename(pdb_file.filename, "upload.pdb")

    pe_name: str | None = None
    if pe_file is not None:
        pe_name = _sanitize_upload_basename(pe_file.filename, "upload.dll")

    project_uuid = project.id
    effective_app, eff_storage = await get_effective_storage(session, storage, base_settings)
    await session.rollback()
    exec_fn = getattr(request.app.state, "exec_fn", None)

    try:
        result = await symbol_service.ingest_symbol(
            session=session,
            storage=eff_storage,
            settings=effective_app,
            project_id=project_uuid,
            app_version=cleaned_version,
            pdb_filename=pdb_name,
            pdb_bytes_or_path=pdb_file.file,
            pe_filename=pe_name,
            pe_bytes_or_path=pe_file.file if pe_file is not None else None,
            exec_fn=exec_fn,
        )
    except (StorageCapacityError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail=str(exc),
        ) from exc
    except SymbolConversionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    sym_read = SymbolRead.model_validate(result.symbol)
    return SymbolUploadResponse(
        **sym_read.model_dump(),
        deduplicated=result.deduplicated,
        upgraded=result.upgraded,
    )


@router.post(
    "/public/upload",
    response_model=SymbolUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_public_symbol(
    request: Request,
    pdb_file: UploadFile = File(...),
    pe_file: UploadFile | None = File(default=None),
    auto_resymbolicate: bool = Form(default=False),
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
    dispatcher: TaskDispatcher = Depends(get_dispatcher),
    base_settings: Settings = Depends(get_app_settings),
) -> SymbolUploadResponse:
    """Manually complete the global Public Symbol Store without binding project or app_version."""
    pdb_name = _sanitize_upload_basename(pdb_file.filename, "upload.pdb")
    pe_name: str | None = None
    if pe_file is not None:
        pe_name = _sanitize_upload_basename(pe_file.filename, "upload.dll")

    effective_app, eff_storage = await get_effective_storage(session, storage, base_settings)
    await session.rollback()
    exec_fn = getattr(request.app.state, "exec_fn", None)

    try:
        result = await symbol_service.ingest_public_symbol(
            session=session,
            storage=eff_storage,
            settings=effective_app,
            pdb_filename=pdb_name,
            pdb_bytes_or_path=pdb_file.file,
            pe_filename=pe_name,
            pe_bytes_or_path=pe_file.file if pe_file is not None else None,
            source="upload",
            exec_fn=exec_fn,
        )
    except (StorageCapacityError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail=str(exc),
        ) from exc
    except SymbolConversionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    queued_ids: list[uuid.UUID] = []
    active_ids: list[uuid.UUID] = []
    if auto_resymbolicate:
        queued_ids, active_ids = await _queue_resymbolication_with_stats(
            session=session,
            dispatcher=dispatcher,
            storage=eff_storage,
            debug_ids={result.symbol.debug_id},
        )

    sym_read = SymbolRead.model_validate(result.symbol)
    return SymbolUploadResponse(
        **sym_read.model_dump(),
        deduplicated=result.deduplicated,
        upgraded=result.upgraded,
        resymbolicate_queued_count=len(queued_ids),
        resymbolicate_crash_ids=queued_ids,
        resymbolicate_skipped_active_count=len(active_ids),
        resymbolicate_active_crash_ids=active_ids,
    )


async def _resolve_public_sync_candidates(
    *,
    payload: PublicSymbolSyncRequest,
    session: AsyncSession,
    eff_storage: StorageManager,
    effective_app: Settings,
) -> tuple[
    list[ms_symbol_service.WindowsSymbolCandidate],
    list[ms_symbol_service.MSFetchItemResult],
    bool,
]:
    """Resolve explicit or full missing-Windows sync candidates without burning slots on cache."""
    seen_keys: set[tuple[str, str]] = set()
    explicit_targets = payload.mode == "targets"

    if explicit_targets:
        if not payload.targets:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="targets must contain at least one symbol when mode='targets'",
            )
        if len(payload.targets) > MAX_EXPLICIT_SYNC_TARGETS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"At most {MAX_EXPLICIT_SYNC_TARGETS} targets can be synced in a single request"
                ),
            )
        candidates: list[ms_symbol_service.WindowsSymbolCandidate] = []
        for target in payload.targets:
            name_input = (target.debug_file or target.module_name or target.code_file or "").strip()
            if not name_input:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Each target requires debug_file or module_name",
                )
            try:
                cand = ms_symbol_service.parse_manual_ms_target(
                    name_input,
                    target.debug_id,
                    age=target.age,
                    code_file=target.code_file,
                    code_id=target.code_id,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=str(exc),
                ) from exc
            dedup = (cand.debug_file.lower(), cand.debug_id.upper())
            if dedup not in seen_keys:
                seen_keys.add(dedup)
                candidates.append(cand)
        return candidates, [], True

    missing_items, _ = await aggregate_missing_symbols(
        session,
        project_id=payload.project_id,
        limit=None,
        offset=0,
        storage=eff_storage,
    )
    uncached_candidates: list[ms_symbol_service.WindowsSymbolCandidate] = []
    upgrade_candidates: list[ms_symbol_service.WindowsSymbolCandidate] = []
    pre_cached_items: list[ms_symbol_service.MSFetchItemResult] = []

    for item in missing_items:
        if not item.is_windows_system:
            continue
        clean_df = ms_symbol_service.normalize_debug_file_name(item.debug_file)
        if not ms_symbol_service._is_valid_pdb_identity(clean_df, item.debug_id):
            continue
        dedup = (clean_df.lower(), item.debug_id.upper())
        if dedup in seen_keys:
            continue
        seen_keys.add(dedup)

        cand = ms_symbol_service.WindowsSymbolCandidate(
            module_name=item.module_name or clean_df,
            debug_file=clean_df,
            debug_id=item.debug_id.upper(),
            code_file=item.code_file,
            code_id=item.code_id,
            filename=item.code_file,
        )

        if item.is_available_locally and item.local_scope == "project":
            pre_cached_items.append(
                ms_symbol_service.MSFetchItemResult(
                    module_name=cand.module_name,
                    debug_file=cand.debug_file,
                    debug_id=cand.debug_id,
                    status="cached_hit",
                    detail="Already cached locally",
                    has_pe_unwind=False,
                )
            )
            continue

        cached, cached_scope, cached_path = ms_symbol_service.has_cached_symbol_detail(
            eff_storage,
            clean_df,
            item.debug_id,
            project_id=payload.project_id,
        )
        if cached:
            existing_has_unwind = (
                await asyncio.to_thread(ms_symbol_service._sym_file_has_unwind, cached_path)
                if cached_path is not None
                else False
            )
            if (
                not payload.force
                or cached_scope == "project"
                or existing_has_unwind
                or not effective_app.MS_SYMBOL_FETCH_PE_UNWIND
            ):
                pre_cached_items.append(
                    ms_symbol_service.MSFetchItemResult(
                        module_name=cand.module_name,
                        debug_file=cand.debug_file,
                        debug_id=cand.debug_id,
                        status="cached_hit",
                        detail="Already cached locally",
                        has_pe_unwind=existing_has_unwind,
                    )
                )
                continue
            upgrade_candidates.append(cand)
        else:
            uncached_candidates.append(cand)

    cap = (
        payload.max_targets
        if payload.max_targets is not None
        else DEFAULT_MISSING_SYNC_CEILING
    )
    candidates = (uncached_candidates + upgrade_candidates)[:cap]
    return candidates, pre_cached_items, False


def _serialize_sync_item(item: ms_symbol_service.MSFetchItemResult) -> PublicSymbolSyncItemResult:
    return PublicSymbolSyncItemResult(
        module_name=item.module_name,
        debug_file=item.debug_file,
        debug_id=item.debug_id,
        status=item.status,  # type: ignore[arg-type]
        detail=item.detail,
        has_pe_unwind=item.has_pe_unwind,
        symbol_id=item.symbol_id,
    )


def _build_public_sync_response(
    summary: ms_symbol_service.MSFetchSummary,
    pre_cached_items: list[ms_symbol_service.MSFetchItemResult],
    queued_ids: list[uuid.UUID],
    active_ids: list[uuid.UUID] | None = None,
) -> PublicSymbolSyncResponse:
    all_items = [*summary.items, *pre_cached_items]
    skipped_active = list(active_ids or [])
    return PublicSymbolSyncResponse(
        attempted=summary.attempted,
        imported=summary.imported,
        upgraded=summary.upgraded,
        cached_hit=summary.cached_hit + len(pre_cached_items),
        not_found=summary.not_found,
        skipped=summary.skipped,
        failed=summary.failed,
        circuit_open=summary.circuit_open,
        resymbolicate_queued_count=len(queued_ids),
        resymbolicate_crash_ids=queued_ids,
        resymbolicate_skipped_active_count=len(skipped_active),
        resymbolicate_active_crash_ids=skipped_active,
        results=[_serialize_sync_item(item) for item in all_items],
    )


@router.post(
    "/public/sync",
    response_model=PublicSymbolSyncResponse,
    status_code=status.HTTP_200_OK,
)
async def sync_public_symbols(
    request: Request,
    payload: PublicSymbolSyncRequest,
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
    dispatcher: TaskDispatcher = Depends(get_dispatcher),
    base_settings: Settings = Depends(get_app_settings),
) -> PublicSymbolSyncResponse:
    """Actively fetch Microsoft public symbols into the global Public Symbol Store."""
    effective_app, eff_storage = await get_effective_storage(session, storage, base_settings)
    await session.rollback()

    candidates, pre_cached_items, explicit_targets = await _resolve_public_sync_candidates(
        payload=payload,
        session=session,
        eff_storage=eff_storage,
        effective_app=effective_app,
    )

    if not candidates and not pre_cached_items:
        return PublicSymbolSyncResponse()

    exec_fn = getattr(request.app.state, "exec_fn", None)
    cab_exec_fn = getattr(request.app.state, "cab_exec_fn", None)
    http_transport = getattr(request.app.state, "http_transport", None)

    if candidates:
        summary = await ms_symbol_service.fetch_and_import_windows_symbols(
            candidates,
            eff_storage,
            effective_app,
            project_id=payload.project_id,
            session=session,
            explicit_targets=explicit_targets,
            force=payload.force,
            bypass_auto_fetch_flag=True,
            allow_winbindex=True,
            max_concurrency=ACTIVE_MS_SYNC_CONCURRENCY,
            exec_fn=exec_fn,
            cab_exec_fn=cab_exec_fn,
            http_transport=http_transport,
        )
    else:
        summary = ms_symbol_service.MSFetchSummary()

    if summary.cached_hit > 0 or pre_cached_items:
        await symbol_service.reconcile_public_symbol_cache(session, eff_storage)

    queued_ids: list[uuid.UUID] = []
    active_ids: list[uuid.UUID] = []
    if payload.auto_resymbolicate:
        ready_debug_ids = {
            item.debug_id.upper()
            for item in (*summary.items, *pre_cached_items)
            if item.status in {"imported", "upgraded", "cached_hit"}
        }
        if ready_debug_ids:
            queued_ids, active_ids = await _queue_resymbolication_with_stats(
                session=session,
                dispatcher=dispatcher,
                storage=eff_storage,
                project_id=payload.project_id,
                debug_ids=ready_debug_ids,
            )

    return _build_public_sync_response(summary, pre_cached_items, queued_ids, active_ids)


@router.post("/public/sync/stream")
async def sync_public_symbols_stream(
    request: Request,
    payload: PublicSymbolSyncRequest,
    storage: StorageManager = Depends(get_storage),
    dispatcher: TaskDispatcher = Depends(get_dispatcher),
    base_settings: Settings = Depends(get_app_settings),
) -> StreamingResponse:
    """Stream real-time per-symbol progress (SSE) while syncing Microsoft public symbols."""
    session_factory = getattr(request.app.state, "session_factory", None) or get_session_factory()
    async with session_factory() as init_session:
        effective_app, eff_storage = await get_effective_storage(
            init_session, storage, base_settings
        )
        candidates, pre_cached_items, explicit_targets = await _resolve_public_sync_candidates(
            payload=payload,
            session=init_session,
            eff_storage=eff_storage,
            effective_app=effective_app,
        )

    exec_fn = getattr(request.app.state, "exec_fn", None)
    cab_exec_fn = getattr(request.app.state, "cab_exec_fn", None)
    http_transport = getattr(request.app.state, "http_transport", None)

    def _format_event(data: dict[str, Any]) -> str:
        return f"event: sync\ndata: {json.dumps(data)}\n\n"

    async def event_generator():
        total = len(candidates)
        already_cached = len(pre_cached_items)
        counters: dict[str, int] = {
            "completed": 0,
            "imported": 0,
            "upgraded": 0,
            "cached_hit": already_cached,
            "not_found": 0,
            "skipped": 0,
            "failed": 0,
        }
        active_map: dict[tuple[str, str], str] = {}

        yield _format_event(
            {
                "type": "start",
                "total": total,
                "already_cached": already_cached,
                "active_modules": [],
                "circuit_open": False,
                **counters,
            }
        )

        if not candidates:
            queued_ids: list[uuid.UUID] = []
            active_ids: list[uuid.UUID] = []
            if pre_cached_items:
                async with session_factory() as own_session:
                    await symbol_service.reconcile_public_symbol_cache(own_session, eff_storage)
                    if payload.auto_resymbolicate:
                        ready_debug_ids = {
                            item.debug_id.upper() for item in pre_cached_items
                        }
                        if ready_debug_ids:
                            queued_ids, active_ids = await _queue_resymbolication_with_stats(
                                session=own_session,
                                dispatcher=dispatcher,
                                storage=eff_storage,
                                project_id=payload.project_id,
                                debug_ids=ready_debug_ids,
                            )
            response = _build_public_sync_response(
                ms_symbol_service.MSFetchSummary(),
                pre_cached_items,
                queued_ids,
                active_ids,
            )
            yield _format_event(
                {
                    "type": "complete",
                    "total": 0,
                    "completed": 0,
                    "already_cached": already_cached,
                    "summary": response.model_dump(mode="json"),
                }
            )
            return

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def _on_item_start(cand: ms_symbol_service.WindowsSymbolCandidate) -> None:
            active_map[(cand.debug_file.lower(), cand.debug_id.upper())] = cand.debug_file
            await queue.put(
                {
                    "type": "item_start",
                    "total": total,
                    "already_cached": already_cached,
                    "current": {
                        "module_name": cand.module_name,
                        "debug_file": cand.debug_file,
                        "debug_id": cand.debug_id,
                    },
                    "active_modules": list(active_map.values()),
                    "circuit_open": ms_symbol_service.is_circuit_open(eff_storage),
                    **counters,
                }
            )

        async def _on_item_done(item: ms_symbol_service.MSFetchItemResult) -> None:
            active_map.pop((item.debug_file.lower(), item.debug_id.upper()), None)
            counters["completed"] += 1
            if item.status == "imported":
                counters["imported"] += 1
            elif item.status == "upgraded":
                counters["upgraded"] += 1
            elif item.status == "cached_hit":
                counters["cached_hit"] += 1
            elif item.status == "not_found":
                counters["not_found"] += 1
            elif item.status in {"network_error", "failed"}:
                counters["failed"] += 1
            else:
                counters["skipped"] += 1
            await queue.put(
                {
                    "type": "progress",
                    "total": total,
                    "already_cached": already_cached,
                    "active_modules": list(active_map.values()),
                    "circuit_open": ms_symbol_service.is_circuit_open(eff_storage),
                    "item": _serialize_sync_item(item).model_dump(mode="json"),
                    **counters,
                }
            )

        async def _run_sync() -> None:
            try:
                summary = await ms_symbol_service.fetch_and_import_windows_symbols(
                    candidates,
                    eff_storage,
                    effective_app,
                    project_id=payload.project_id,
                    session_factory=session_factory,
                    explicit_targets=explicit_targets,
                    force=payload.force,
                    bypass_auto_fetch_flag=True,
                    allow_winbindex=True,
                    max_concurrency=ACTIVE_MS_SYNC_CONCURRENCY,
                    on_item_start=_on_item_start,
                    on_item_done=_on_item_done,
                    exec_fn=exec_fn,
                    cab_exec_fn=cab_exec_fn,
                    http_transport=http_transport,
                )

                queued_ids: list[uuid.UUID] = []
                active_ids: list[uuid.UUID] = []
                async with session_factory() as own_session:
                    if summary.cached_hit > 0 or pre_cached_items:
                        await symbol_service.reconcile_public_symbol_cache(
                            own_session, eff_storage
                        )
                    if payload.auto_resymbolicate:
                        ready_debug_ids = {
                            item.debug_id.upper()
                            for item in (*summary.items, *pre_cached_items)
                            if item.status in {"imported", "upgraded", "cached_hit"}
                        }
                        if ready_debug_ids:
                            queued_ids, active_ids = await _queue_resymbolication_with_stats(
                                session=own_session,
                                dispatcher=dispatcher,
                                storage=eff_storage,
                                project_id=payload.project_id,
                                debug_ids=ready_debug_ids,
                            )

                response = _build_public_sync_response(
                    summary, pre_cached_items, queued_ids, active_ids
                )
                await queue.put(
                    {
                        "type": "complete",
                        "total": total,
                        "completed": counters["completed"],
                        "already_cached": already_cached,
                        "summary": response.model_dump(mode="json"),
                    }
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await queue.put(
                    {
                        "type": "error",
                        "detail": str(exc) or type(exc).__name__,
                    }
                )
            finally:
                await queue.put(None)

        sync_task = asyncio.create_task(_run_sync())
        last_keepalive = time.monotonic()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=2.0)
                except TimeoutError:
                    if await request.is_disconnected():
                        break
                    now = time.monotonic()
                    if now - last_keepalive >= 15.0:
                        last_keepalive = now
                        yield ": keepalive\n\n"
                    continue
                if evt is None:
                    break
                last_keepalive = time.monotonic()
                yield _format_event(evt)
        finally:
            if not sync_task.done():
                sync_task.cancel()
                try:
                    await sync_task
                except asyncio.CancelledError:
                    pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("", response_model=SymbolListResponse)
async def list_symbols(
    project_id: uuid.UUID | None = None,
    scope: Literal["project", "public", "all"] | None = Query(default=None),
    source: str | None = Query(default=None),
    app_version: str | None = None,
    q: str | None = None,
    has_pe_unwind: bool | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
) -> SymbolListResponse:
    if scope in {"public", "all"}:
        await symbol_service.reconcile_public_symbol_cache(session, storage)

    filters = []
    if scope == "public":
        filters.append(Symbol.project_id.is_(None))
    elif scope == "all":
        if project_id is not None:
            filters.append(or_(Symbol.project_id == project_id, Symbol.project_id.is_(None)))
    else:
        filters.append(Symbol.project_id.is_not(None))
        if project_id is not None:
            filters.append(Symbol.project_id == project_id)

    if source is not None and source.strip():
        filters.append(Symbol.source == source.strip())
    if scope != "public" and app_version is not None and app_version.strip():
        filters.append(Symbol.app_version == app_version.strip())
    if has_pe_unwind is not None:
        filters.append(Symbol.has_pe_unwind == has_pe_unwind)
    if q is not None and q.strip():
        pattern = f"%{q.strip()}%"
        filters.append(
            or_(
                Symbol.debug_file.ilike(pattern),
                Symbol.module_name.ilike(pattern),
                Symbol.code_file.ilike(pattern),
                Symbol.debug_id.ilike(pattern),
            )
        )

    count_stmt = select(func.count()).select_from(Symbol)
    list_stmt = select(Symbol)
    if filters:
        count_stmt = count_stmt.where(*filters)
        list_stmt = list_stmt.where(*filters)

    total = int((await session.execute(count_stmt)).scalar_one())
    rows = (
        await session.execute(
            list_stmt.order_by(Symbol.created_at.desc()).offset(offset).limit(limit)
        )
    ).scalars().all()
    return SymbolListResponse(
        items=[SymbolRead.model_validate(row) for row in rows],
        total=total,
    )


@router.get("/missing", response_model=list[MissingSymbolQueueItem])
async def list_missing_symbols(
    response: Response,
    project_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
) -> list[MissingSymbolQueueItem]:
    items, total = await aggregate_missing_symbols(
        session,
        project_id=project_id,
        limit=limit,
        offset=offset,
        storage=storage,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/resymbolicate/status", response_model=ResymbolicateStatusResponse)
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


@router.post(
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


@router.delete("/{symbol_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_symbol(
    symbol_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
) -> Response:
    sym = await session.get(Symbol, symbol_id)
    if sym is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Symbol not found")

    sym_path = sym.sym_path
    sym_partition = (
        "symbols/public_cache"
        if sym.project_id is None or (sym_path or "").startswith("symbols/public_cache/")
        else "symbols/sym"
    )
    raw_archive_path = sym.raw_archive_path
    code_id = sym.code_id
    code_file = sym.code_file

    await session.delete(sym)
    await session.flush()

    if sym_path:
        sym_ref_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(Symbol).where(Symbol.sym_path == sym_path)
                )
            ).scalar_one()
        )
        if sym_ref_count == 0:
            _unlink_and_prune(storage, sym_path, sym_partition)

    if raw_archive_path:
        raw_ref_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Symbol)
                    .where(Symbol.raw_archive_path == raw_archive_path)
                )
            ).scalar_one()
        )
        if raw_ref_count == 0:
            _unlink_and_prune(storage, raw_archive_path, "symbols/raw")

    if code_id and code_file:
        pe_raw_rel = f"symbols/raw/{code_id}/{code_file}.zst"
        pe_ref_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Symbol)
                    .where(
                        or_(
                            and_(
                                Symbol.code_id == code_id,
                                Symbol.code_file == code_file,
                                Symbol.has_pe_unwind.is_(True),
                            ),
                            Symbol.raw_archive_path == pe_raw_rel,
                        )
                    )
                )
            ).scalar_one()
        )
        if pe_ref_count == 0:
            _unlink_and_prune(storage, pe_raw_rel, "symbols/raw")

    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@symsrv_router.get("/pingme.txt", response_class=PlainTextResponse)
async def symsrv_pingme() -> PlainTextResponse:
    return PlainTextResponse("", status_code=status.HTTP_200_OK)


@symsrv_router.get("/index2.txt")
async def symsrv_index2() -> Response:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


@symsrv_router.api_route(
    "/{filename}/{symbol_id}/{target_filename}",
    methods=["GET", "HEAD"],
)
async def symsrv_fetch_symbol(
    filename: str,
    symbol_id: str,
    target_filename: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
) -> Response:
    if target_filename.lower() != filename.lower():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Symbol not found")

    resolved = await resolve_symsrv_file(session, storage, filename, symbol_id)
    if resolved is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Symbol not found")

    zst_path, _ = resolved
    if not zst_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Symbol file missing")

    if request.method == "HEAD":
        return Response(
            content=b"",
            status_code=status.HTTP_200_OK,
            media_type="application/octet-stream",
        )

    return StreamingResponse(
        stream_decompressed_zst(zst_path),
        status_code=status.HTTP_200_OK,
        media_type="application/octet-stream",
    )
