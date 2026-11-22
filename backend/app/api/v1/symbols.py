"""Symbols REST API (/api/v1/symbols) and WinDbg SymSrv (/symsrv) endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from sqlalchemy import and_, func, or_, select
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
from app.schemas.symbol import (
    MissingSymbolQueueItem,
    ResymbolicateRequest,
    ResymbolicateResponse,
    SymbolListResponse,
    SymbolRead,
    SymbolUploadResponse,
)
from app.services import symbol_service
from app.services.storage import StorageError, StorageManager
from app.services.symbol_service import (
    SymbolConversionError,
    resolve_symsrv_file,
    stream_decompressed_zst,
)
from app.workers.worker import TaskDispatcher

router = APIRouter(prefix="/symbols", tags=["symbols"])
symsrv_router = APIRouter(prefix="/symsrv", tags=["symsrv"])


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
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[MissingSymbolQueueItem], int]:
    """Aggregate missing symbols from CrashReport.missing_symbols_json across crashes."""
    stmt = select(
        CrashReport.id,
        CrashReport.missing_symbols_json,
        CrashReport.crash_time,
        CrashReport.created_at,
    ).where(CrashReport.missing_symbols_json.is_not(None))
    if project_id is not None:
        stmt = stmt.where(CrashReport.project_id == project_id)

    rows = (await session.execute(stmt)).all()
    buckets: dict[tuple[str, str], dict[str, Any]] = {}

    for crash_id, missing_list, crash_time, created_at in rows:
        if not isinstance(missing_list, list) or not missing_list:
            continue
        event_ts = _as_utc(crash_time or created_at)
        seen_in_crash: set[tuple[str, str]] = set()
        for entry in missing_list:
            if not isinstance(entry, dict):
                continue
            raw_df = entry.get("debug_file")
            raw_did = entry.get("debug_id")
            debug_file = str(raw_df).strip() if raw_df is not None else ""
            debug_id = str(raw_did).strip().upper() if raw_did is not None else ""
            if not debug_file and not debug_id:
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
                }
            else:
                bucket["affected_crash_ids"].append(crash_id)
                if event_ts > bucket["latest_crash_at"]:
                    bucket["latest_crash_at"] = event_ts
                if not bucket["code_file"] and code_file:
                    bucket["code_file"] = code_file
                if not bucket["code_id"] and code_id:
                    bucket["code_id"] = code_id
                if bucket["module_name"] == "unknown" and module_name != "unknown":
                    bucket["module_name"] = module_name

    items = [
        MissingSymbolQueueItem(
            debug_file=b["debug_file"],
            debug_id=b["debug_id"],
            module_name=b["module_name"],
            code_file=b["code_file"],
            code_id=b["code_id"],
            crash_count=len(b["affected_crash_ids"]),
            latest_crash_at=b["latest_crash_at"],
            affected_crash_ids=b["affected_crash_ids"],
        )
        for b in buckets.values()
    ]
    items.sort(
        key=lambda item: (
            -item.crash_count,
            -item.latest_crash_at.timestamp(),
            item.debug_file.lower(),
            item.debug_id,
        )
    )
    total = len(items)
    return items[offset : offset + limit], total


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

    pdb_bytes = await pdb_file.read()
    pdb_name = _sanitize_upload_basename(pdb_file.filename, "upload.pdb")

    pe_bytes: bytes | None = None
    pe_name: str | None = None
    if pe_file is not None:
        pe_bytes = await pe_file.read()
        pe_name = _sanitize_upload_basename(pe_file.filename, "upload.dll")

    effective_app, eff_storage = await get_effective_storage(session, storage, base_settings)
    exec_fn = getattr(request.app.state, "exec_fn", None)

    try:
        result = await symbol_service.ingest_symbol(
            session=session,
            storage=eff_storage,
            settings=effective_app,
            project_id=project.id,
            app_version=cleaned_version,
            pdb_filename=pdb_name,
            pdb_bytes_or_path=pdb_bytes,
            pe_filename=pe_name,
            pe_bytes_or_path=pe_bytes,
            exec_fn=exec_fn,
        )
    except SymbolConversionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    sym_read = SymbolRead.model_validate(result.symbol)
    return SymbolUploadResponse(
        **sym_read.model_dump(),
        deduplicated=result.deduplicated,
    )


@router.get("", response_model=SymbolListResponse)
async def list_symbols(
    project_id: uuid.UUID | None = None,
    app_version: str | None = None,
    q: str | None = None,
    has_pe_unwind: bool | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> SymbolListResponse:
    filters = []
    if project_id is not None:
        filters.append(Symbol.project_id == project_id)
    if app_version is not None and app_version.strip():
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
    project_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> list[MissingSymbolQueueItem]:
    items, _ = await aggregate_missing_symbols(
        session,
        project_id=project_id,
        limit=limit,
        offset=offset,
    )
    return items


@router.post(
    "/resymbolicate",
    response_model=ResymbolicateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resymbolicate_crashes(
    payload: ResymbolicateRequest,
    session: AsyncSession = Depends(get_db),
    dispatcher: TaskDispatcher = Depends(get_dispatcher),
) -> ResymbolicateResponse:
    if payload.crash_ids is not None and len(payload.crash_ids) == 0:
        return ResymbolicateResponse(queued_count=0, crash_ids=[])

    stmt = select(CrashReport.id, CrashReport.missing_symbols_json).where(
        CrashReport.dump_path.is_not(None)
    )
    if payload.crash_ids is not None:
        stmt = stmt.where(CrashReport.id.in_(payload.crash_ids))
    if payload.project_id is not None:
        stmt = stmt.where(CrashReport.project_id == payload.project_id)

    rows = (await session.execute(stmt)).all()
    target_debug_id = payload.debug_id.strip().upper() if payload.debug_id else None

    matched_ids: list[uuid.UUID] = []
    for row_id, missing_json in rows:
        if target_debug_id:
            if not any(
                isinstance(entry, dict)
                and str(entry.get("debug_id") or "").strip().upper() == target_debug_id
                for entry in (missing_json or [])
            ):
                continue
        matched_ids.append(row_id)

    if matched_ids:
        await dispatcher.enqueue(
            "resymbolicate",
            [str(cid) for cid in matched_ids],
            str(payload.project_id) if payload.project_id else None,
            payload.debug_id,
        )

    return ResymbolicateResponse(
        queued_count=len(matched_ids),
        crash_ids=matched_ids,
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
            _unlink_and_prune(storage, sym_path, "symbols/sym")

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
