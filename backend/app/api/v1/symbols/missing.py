"""Missing symbol queue aggregation and listing endpoint."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_storage
from app.schemas.symbol import MissingSymbolQueueItem
from app.services import ms_symbol_service
from app.services.missing_symbols import (
    _ALL_ZERO_DEBUG_ID_RE as _ALL_ZERO_DEBUG_ID_RE,
)
from app.services.missing_symbols import (
    _as_utc as _as_utc,
)
from app.services.missing_symbols import query_missing_symbols_aggregated
from app.services.storage import StorageManager

missing_router = APIRouter()


async def aggregate_missing_symbols(
    session: AsyncSession,
    project_id: uuid.UUID | None = None,
    limit: int | None = 50,
    offset: int = 0,
    storage: StorageManager | None = None,
) -> tuple[list[MissingSymbolQueueItem], int]:
    """Aggregate missing symbols using indexed crash_missing_symbols SQL queries."""
    page_buckets, total = await query_missing_symbols_aggregated(
        session,
        project_id=project_id,
        limit=limit,
        offset=offset,
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


@missing_router.get("/missing", response_model=list[MissingSymbolQueueItem])
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
