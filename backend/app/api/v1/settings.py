"""Settings and storage GC REST API endpoints (/api/v1/settings)."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_app_settings,
    get_db,
    get_effective_storage,
    get_storage,
)
from app.config import Settings
from app.schemas.settings import (
    GCReport,
    GCRequest,
    SystemSettingsRead,
    SystemSettingsUpdate,
)
from app.services import settings_service
from app.services.storage import StorageManager

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=SystemSettingsRead)
async def get_system_settings(
    session: AsyncSession = Depends(get_db),
    base_settings: Settings = Depends(get_app_settings),
) -> SystemSettingsRead:
    effective = await settings_service.get_effective_settings(
        session,
        base_settings=base_settings,
    )
    return SystemSettingsRead.model_validate(effective.to_public_dict())


@router.put("", response_model=SystemSettingsRead)
async def update_system_settings(
    payload: SystemSettingsUpdate,
    session: AsyncSession = Depends(get_db),
    base_settings: Settings = Depends(get_app_settings),
) -> SystemSettingsRead:
    effective = await settings_service.update_settings(
        session,
        updates=payload,
        base_settings=base_settings,
    )
    return SystemSettingsRead.model_validate(effective.to_public_dict())


@router.post("/gc", response_model=GCReport)
async def trigger_storage_gc(
    payload: GCRequest = Body(default_factory=GCRequest),
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
    base_settings: Settings = Depends(get_app_settings),
) -> GCReport:
    _, eff_storage = await get_effective_storage(session, storage, base_settings)
    report = await eff_storage.run_gc(session, force_emergency=payload.force_emergency)
    return GCReport.model_validate(report)
