"""Aggregated API v1 router."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import require_admin
from app.api.v1.crashes import get_crash_status, stream_crash_events, upload_crash
from app.api.v1.crashes import router as crashes_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.issues import router as issues_router
from app.api.v1.logs import router as logs_router
from app.api.v1.projects import router as projects_router
from app.api.v1.settings import router as settings_router
from app.api.v1.symbols import router as symbols_router
from app.api.v1.symbols import upload_symbol

# These handlers authenticate machine requests against the owning project.
_PROJECT_AUTH_ENDPOINTS = (upload_crash, upload_symbol, get_crash_status, stream_crash_events)


async def _require_api_admin(request: Request) -> None:
    if request.scope.get("endpoint") not in _PROJECT_AUTH_ENDPOINTS:
        await require_admin(request)


api_v1_router = APIRouter(dependencies=[Depends(_require_api_admin)])
api_v1_router.include_router(projects_router)
api_v1_router.include_router(symbols_router)
api_v1_router.include_router(crashes_router)
api_v1_router.include_router(logs_router)
api_v1_router.include_router(issues_router)
api_v1_router.include_router(settings_router)
api_v1_router.include_router(dashboard_router)

__all__ = ["api_v1_router"]
