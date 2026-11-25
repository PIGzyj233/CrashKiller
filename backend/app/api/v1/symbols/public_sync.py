"""Microsoft public symbol sync and SSE streaming progress endpoints."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_app_settings,
    get_db,
    get_dispatcher,
    get_effective_storage,
    get_storage,
)
from app.api.v1.symbols.helpers import _queue_resymbolication_with_stats
from app.api.v1.symbols.missing import aggregate_missing_symbols
from app.config import Settings
from app.db.session import get_session_factory
from app.schemas.symbol import (
    PublicSymbolSyncItemResult,
    PublicSymbolSyncRequest,
    PublicSymbolSyncResponse,
)
from app.services import ms_symbol_service, symbol_service
from app.services.storage import StorageManager
from app.workers.worker import TaskDispatcher

public_sync_router = APIRouter()

ACTIVE_MS_SYNC_CONCURRENCY = 4
MAX_EXPLICIT_SYNC_TARGETS = 500
DEFAULT_MISSING_SYNC_CEILING = 2000


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


@public_sync_router.post(
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


@public_sync_router.post("/public/sync/stream")
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
