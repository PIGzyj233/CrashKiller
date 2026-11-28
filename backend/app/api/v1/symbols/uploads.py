"""Project and global public symbol upload endpoints."""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, BinaryIO

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
from fastapi.responses import JSONResponse
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
from app.workers.jobs import COMPUTE_QUEUE_NAME, CRASH_JOB_TIMEOUT_SECONDS
from app.workers.worker import TaskDispatcher

uploads_router = APIRouter()


def _measure_upload_stream(stream: BinaryIO) -> int:
    try:
        pos = stream.tell()
        size = stream.seek(0, os.SEEK_END)
        stream.seek(pos)
        return max(0, int(size))
    except (OSError, AttributeError, ValueError):
        return 0


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
    async_mode: bool = Form(default=False),
    api_key: str | None = Form(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
    dispatcher: TaskDispatcher = Depends(get_dispatcher),
    base_settings: Settings = Depends(get_app_settings),
) -> Any:
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
        if pe_name == pdb_name:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="PDB and PE must have distinct filenames",
            )

    project_uuid = project.id
    effective_app, eff_storage = await get_effective_storage(session, storage, base_settings)

    if async_mode:
        upload_id = uuid.uuid4().hex
        stage_dir_rel = f"staging/symbol_uploads/{upload_id}"
        stage_dir_abs: Path | None = None
        est_pdb = _measure_upload_stream(pdb_file.file)
        est_pe = _measure_upload_stream(pe_file.file) if pe_file is not None else 0
        reserved_bytes = est_pdb + est_pe
        reserved_files = 1 + (1 if pe_file is not None and pe_name is not None else 0)
        staged_committed = False
        async with eff_storage.storage_io_lock(exclusive=False):
            async with eff_storage.async_staging_upload_lease("symupload", upload_id):
                try:
                    await eff_storage.commit_usage_deltas(
                        session,
                        {"staging": reserved_bytes},
                        {"staging": reserved_files},
                        enforce_quota=True,
                        extra_headroom_checks={"symbols/sym": 1},
                    )
                    await session.commit()
                    staged_committed = True

                    stage_dir_abs = eff_storage.resolve_path(stage_dir_rel)
                    pdb_rel = f"{stage_dir_rel}/{pdb_name}"
                    pdb_staged_abs, pdb_size = await asyncio.to_thread(
                        eff_storage.atomic_write_stream, pdb_rel, pdb_file.file
                    )
                    pe_staged_abs: Path | None = None
                    pe_size = 0
                    if pe_file is not None and pe_name is not None:
                        pe_rel = f"{stage_dir_rel}/{pe_name}"
                        pe_staged_abs, pe_size = await asyncio.to_thread(
                            eff_storage.atomic_write_stream, pe_rel, pe_file.file
                        )

                    actual_bytes = pdb_size + pe_size
                    if actual_bytes != reserved_bytes:
                        diff = actual_bytes - reserved_bytes
                        await eff_storage.commit_usage_deltas(
                            session,
                            {"staging": diff},
                            {},
                            enforce_quota=diff > 0,
                        )
                        await session.commit()
                        reserved_bytes = actual_bytes

                    sym_job_id = f"symbol-ingest:{upload_id}"
                    eff_storage.write_staging_queue_lease(
                        stage_dir_rel,
                        ttl_seconds=float(CRASH_JOB_TIMEOUT_SECONDS),
                        job_id=sym_job_id,
                        queue_name=COMPUTE_QUEUE_NAME,
                        mode="memory" if dispatcher.is_memory_mode else "redis",
                    )
                    job_id = await dispatcher.enqueue(
                        "symbol_ingest",
                        str(project_uuid),
                        cleaned_version,
                        str(pdb_staged_abs),
                        pdb_name,
                        str(pe_staged_abs) if pe_staged_abs is not None else None,
                        pe_name,
                        _job_id=sym_job_id,
                    )
                except (StorageCapacityError, OSError) as exc:
                    if stage_dir_abs is not None and stage_dir_abs.exists():
                        shutil.rmtree(stage_dir_abs, ignore_errors=True)
                    if staged_committed and (reserved_bytes > 0 or reserved_files > 0):
                        try:
                            await eff_storage.commit_usage_deltas(
                                session,
                                {"staging": -reserved_bytes},
                                {"staging": -reserved_files},
                                enforce_quota=False,
                            )
                            await session.commit()
                        except Exception:
                            await session.rollback()
                    else:
                        await session.rollback()
                    raise HTTPException(
                        status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                        detail=str(exc),
                    ) from exc
                except BaseException:
                    if stage_dir_abs is not None and stage_dir_abs.exists():
                        shutil.rmtree(stage_dir_abs, ignore_errors=True)
                    if staged_committed and (reserved_bytes > 0 or reserved_files > 0):
                        try:
                            await eff_storage.commit_usage_deltas(
                                session,
                                {"staging": -reserved_bytes},
                                {"staging": -reserved_files},
                                enforce_quota=False,
                            )
                            await session.commit()
                        except Exception:
                            await session.rollback()
                    else:
                        await session.rollback()
                    raise
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "status": "PENDING",
                "job_id": job_id,
                "project_id": str(project_uuid),
                "app_version": cleaned_version,
            },
        )

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
