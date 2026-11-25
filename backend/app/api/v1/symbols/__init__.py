"""Symbols REST API (/api/v1/symbols) and WinDbg SymSrv (/symsrv) endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.symbols.helpers import (
    _queue_resymbolication as _queue_resymbolication,
)
from app.api.v1.symbols.helpers import (
    _queue_resymbolication_with_stats as _queue_resymbolication_with_stats,
)
from app.api.v1.symbols.helpers import (
    _sanitize_upload_basename as _sanitize_upload_basename,
)
from app.api.v1.symbols.helpers import (
    _unlink_and_prune as _unlink_and_prune,
)
from app.api.v1.symbols.inventory import (
    delete_symbol,
    inventory_router,
    list_symbols,
)
from app.api.v1.symbols.missing import (
    _ALL_ZERO_DEBUG_ID_RE as _ALL_ZERO_DEBUG_ID_RE,
)
from app.api.v1.symbols.missing import (
    _as_utc as _as_utc,
)
from app.api.v1.symbols.missing import (
    aggregate_missing_symbols,
    list_missing_symbols,
    missing_router,
)
from app.api.v1.symbols.public_sync import (
    ACTIVE_MS_SYNC_CONCURRENCY,
    DEFAULT_MISSING_SYNC_CEILING,
    MAX_EXPLICIT_SYNC_TARGETS,
    public_sync_router,
    sync_public_symbols,
    sync_public_symbols_stream,
)
from app.api.v1.symbols.public_sync import (
    _build_public_sync_response as _build_public_sync_response,
)
from app.api.v1.symbols.public_sync import (
    _resolve_public_sync_candidates as _resolve_public_sync_candidates,
)
from app.api.v1.symbols.public_sync import (
    _serialize_sync_item as _serialize_sync_item,
)
from app.api.v1.symbols.resymbolicate import (
    get_resymbolicate_status,
    resymbolicate_crashes,
    resymbolicate_router,
)
from app.api.v1.symbols.symsrv import (
    symsrv_fetch_symbol,
    symsrv_index2,
    symsrv_pingme,
    symsrv_router,
)
from app.api.v1.symbols.uploads import (
    upload_public_symbol,
    upload_symbol,
    uploads_router,
)
from app.schemas.symbol import SymbolListResponse

router = APIRouter(prefix="/symbols", tags=["symbols"])
router.include_router(uploads_router)
router.include_router(public_sync_router)
router.get("", response_model=SymbolListResponse)(list_symbols)
router.include_router(missing_router)
router.include_router(resymbolicate_router)
router.include_router(inventory_router)

__all__ = [
    "ACTIVE_MS_SYNC_CONCURRENCY",
    "DEFAULT_MISSING_SYNC_CEILING",
    "MAX_EXPLICIT_SYNC_TARGETS",
    "aggregate_missing_symbols",
    "delete_symbol",
    "get_resymbolicate_status",
    "list_missing_symbols",
    "list_symbols",
    "resymbolicate_crashes",
    "router",
    "sync_public_symbols",
    "sync_public_symbols_stream",
    "symsrv_fetch_symbol",
    "symsrv_index2",
    "symsrv_pingme",
    "symsrv_router",
    "upload_public_symbol",
    "upload_symbol",
]
