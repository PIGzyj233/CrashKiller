"""Aggregated API v1 router."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.crashes import router as crashes_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.issues import router as issues_router
from app.api.v1.logs import router as logs_router
from app.api.v1.projects import router as projects_router
from app.api.v1.settings import router as settings_router
from app.api.v1.symbols import router as symbols_router

api_v1_router = APIRouter()
api_v1_router.include_router(projects_router)
api_v1_router.include_router(symbols_router)
api_v1_router.include_router(crashes_router)
api_v1_router.include_router(logs_router)
api_v1_router.include_router(issues_router)
api_v1_router.include_router(settings_router)
api_v1_router.include_router(dashboard_router)

__all__ = ["api_v1_router"]
