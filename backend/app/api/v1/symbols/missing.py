"""Missing symbol queue aggregation and listing endpoint."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_storage
from app.db.models import CrashReport
from app.schemas.symbol import MissingSymbolQueueItem
from app.services import ms_symbol_service
from app.services.storage import StorageManager

missing_router = APIRouter()

_ALL_ZERO_DEBUG_ID_RE = re.compile(r"^0+$")


def _as_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.fromtimestamp(0, tz=UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


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
