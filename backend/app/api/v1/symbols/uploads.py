"""Project and global public symbol upload endpoints."""

from __future__ import annotations

import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_app_settings,
    get_db,
    get_dispatcher,
    get_effective_storage,
    get_storage,
    verify_project_upload_auth,
)
from app.api.v1.symbols.helpers import (
    _queue_resymbolication_with_stats,
    _sanitize_upload_basename,
)
from app.config import Settings
from app.schemas.symbol import SymbolRead, SymbolUploadResponse
from app.services import symbol_service
from app.services.storage import StorageCapacityError, StorageManager
from app.services.symbol_service import SymbolConversionError
from app.workers.worker import TaskDispatcher

uploads_router = APIRouter()


@uploads_router.post(
    "/upload",
    response_model=SymbolUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
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


@uploads_router.post(
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
