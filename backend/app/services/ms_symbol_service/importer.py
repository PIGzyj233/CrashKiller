"""Orchestration for fetching, converting, and persisting Windows/public MSDL symbols."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.services.ms_symbol_service.cache import (
    _public_cache_has_room,
    _record_circuit_network_failure,
    _record_circuit_success,
    _sym_file_has_unwind,
    _write_negative_cache,
    clear_negative_cache,
    has_cached_symbol_detail,
    is_circuit_open,
    is_negative_cached,
    reset_circuit_breaker,
)
from app.services.ms_symbol_service.constants import (
    MAX_MS_FETCH_CONCURRENCY,
    PDB_MAGICS,
    SYMSRV_USER_AGENT,
    TRANSIENT_MISS_TTL_SECONDS,
)
from app.services.ms_symbol_service.downloader import (
    _download_matching_pe,
    _download_symsrv_artifact,
)
from app.services.ms_symbol_service.identity import (
    _is_safe_segment,
    _is_valid_pdb_identity,
    _public_cache_sym_rel,
    is_windows_system_module,
    split_public_symbol_urls,
)
from app.services.ms_symbol_service.models import (
    MSFetchItemResult,
    MSFetchSummary,
    WindowsSymbolCandidate,
)
from app.services.storage import StorageManager
from app.services.symbol_service import (
    SymbolConversionError,
    _resolve_in_partition,
    parse_sym_header,
    run_dump_syms,
    store_public_symbol,
)

logger = logging.getLogger(__name__)


async def _persist_public_msdl_symbol(
    *,
    session: AsyncSession | None,
    session_factory: Any,
    db_lock: asyncio.Lock | None,
    storage: StorageManager,
    settings: Settings,
    sym_bytes: bytes,
    header: Any,
    staged_pdb: Path | None,
    staged_pe: Path | None,
) -> tuple[uuid.UUID | None, bool]:
    """Persist converted MSDL .sym into DB (when session/factory available) and disk."""
    if session is not None:
        if db_lock is not None:
            async with db_lock:
                res = await store_public_symbol(
                    session,
                    storage,
                    settings,
                    sym_bytes=sym_bytes,
                    header=header,
                    source="msdl",
                    staged_pdb=staged_pdb,
                    staged_pe=staged_pe,
                )
        else:
            res = await store_public_symbol(
                session,
                storage,
                settings,
                sym_bytes=sym_bytes,
                header=header,
                source="msdl",
                staged_pdb=staged_pdb,
                staged_pe=staged_pe,
            )
        return res.symbol.id, res.upgraded

    if session_factory is not None:
        async def _write_with_own_session() -> tuple[uuid.UUID | None, bool]:
            async with session_factory() as own_session:
                res = await store_public_symbol(
                    own_session,
                    storage,
                    settings,
                    sym_bytes=sym_bytes,
                    header=header,
                    source="msdl",
                    staged_pdb=staged_pdb,
                    staged_pe=staged_pe,
                )
                return res.symbol.id, res.upgraded

        if db_lock is not None:
            async with db_lock:
                return await _write_with_own_session()
        return await _write_with_own_session()

    rel_sym_path = _public_cache_sym_rel(header.debug_file, header.debug_id)
    _resolve_in_partition(storage, "symbols/public_cache", rel_sym_path)
    await asyncio.to_thread(storage.atomic_write_bytes, rel_sym_path, sym_bytes)
    return None, False


async def _import_single_candidate(
    *,
    candidate: WindowsSymbolCandidate,
    symsrv_urls: list[str],
    client: httpx.AsyncClient,
    storage: StorageManager,
    settings: Settings,
    project_id: uuid.UUID | str | None,
    exec_fn: Any,
    cab_exec_fn: Any,
    session: AsyncSession | None = None,
    session_factory: Any = None,
    db_lock: asyncio.Lock | None = None,
    explicit_targets: bool = False,
    force: bool = False,
    allow_winbindex: bool = False,
) -> MSFetchItemResult:
    """Import one Windows/public symbol into symbols/public_cache (and DB if provided)."""

    def _result(
        status: str,
        detail: str | None = None,
        *,
        has_pe_unwind: bool = False,
        symbol_id: uuid.UUID | None = None,
    ) -> MSFetchItemResult:
        return MSFetchItemResult(
            module_name=candidate.module_name,
            debug_file=candidate.debug_file,
            debug_id=candidate.debug_id,
            status=status,
            detail=detail,
            has_pe_unwind=has_pe_unwind,
            symbol_id=symbol_id,
        )

    if not _is_valid_pdb_identity(candidate.debug_file, candidate.debug_id):
        return _result("skipped", "Invalid PDB filename or debug_id")
    if not explicit_targets and not is_windows_system_module(
        filename=candidate.filename,
        code_file=candidate.code_file,
        module_name=candidate.module_name,
        debug_file=candidate.debug_file,
    ):
        return _result("skipped", "Not recognized as a Windows system module")

    if force:
        clear_negative_cache(storage, candidate.debug_file, candidate.debug_id)

    cached, cached_scope, cached_path = has_cached_symbol_detail(
        storage, candidate.debug_file, candidate.debug_id, project_id=project_id
    )
    existing_has_unwind = (
        await asyncio.to_thread(_sym_file_has_unwind, cached_path)
        if cached and cached_path is not None
        else False
    )
    if cached and (
        not force
        or cached_scope == "project"
        or existing_has_unwind
        or not settings.MS_SYMBOL_FETCH_PE_UNWIND
    ):
        return _result(
            "cached_hit",
            "Already cached locally",
            has_pe_unwind=existing_has_unwind,
        )

    if not force and is_negative_cached(storage, candidate.debug_file, candidate.debug_id):
        return _result("skipped", "Negative-cached from recent miss")
    if not force and is_circuit_open(storage):
        return _result("skipped", "MSDL circuit breaker is open")
    if not _public_cache_has_room(storage):
        logger.warning(
            "Skipping MSDL import for %s: public_cache quota or host floor reached",
            candidate.debug_file,
        )
        return _result("skipped", "Public symbol cache quota or host disk floor reached")

    was_upgrade = cached and cached_scope == "public" and not existing_has_unwind
    lock_name = f"msdl-{candidate.debug_id.lower()[:40]}"
    async with storage.shared_lock(lock_name):
        # Re-check after acquiring per-symbol lock so concurrent workers dedupe cleanly.
        cached, cached_scope, cached_path = has_cached_symbol_detail(
            storage, candidate.debug_file, candidate.debug_id, project_id=project_id
        )
        existing_has_unwind = (
            await asyncio.to_thread(_sym_file_has_unwind, cached_path)
            if cached and cached_path is not None
            else False
        )
        if cached and (
            not force
            or cached_scope == "project"
            or existing_has_unwind
            or not settings.MS_SYMBOL_FETCH_PE_UNWIND
        ):
            return _result(
                "cached_hit",
                "Already cached locally",
                has_pe_unwind=existing_has_unwind,
            )
        if not force and is_negative_cached(storage, candidate.debug_file, candidate.debug_id):
            return _result("skipped", "Negative-cached from recent miss")
        if (not force and is_circuit_open(storage)) or not _public_cache_has_room(storage):
            return _result("skipped", "Skipped due to circuit breaker or storage quota")

        timeout_budget = max(5.0, float(settings.MS_SYMBOL_FETCH_TIMEOUT_SECONDS))
        max_pdb_bytes = max(1, int(settings.MS_SYMBOL_MAX_PDB_MB)) * 1024 * 1024
        max_pe_bytes = max(1, int(settings.MS_SYMBOL_MAX_PE_MB)) * 1024 * 1024
        neg_ttl = max(0, int(settings.MS_SYMBOL_NEGATIVE_CACHE_HOURS)) * 3600

        with storage.staging_workspace("msdl") as staging_dir:
            pdb_path, pdb_outcome, pdb_detail = await _download_symsrv_artifact(
                client=client,
                symsrv_urls=symsrv_urls,
                filename=candidate.debug_file,
                symbol_id=candidate.debug_id,
                staging_dir=staging_dir,
                max_bytes=max_pdb_bytes,
                expected_magics=PDB_MAGICS,
                timeout_seconds=timeout_budget,
                settings=settings,
                exec_fn=cab_exec_fn,
            )

            if pdb_outcome == "network_error":
                tripped = _record_circuit_network_failure(storage, pdb_detail)
                logger.warning(
                    "MSDL network error for %s/%s: %s (circuit_open=%s)",
                    candidate.debug_file,
                    candidate.debug_id,
                    pdb_detail,
                    tripped,
                )
                return _result("network_error", pdb_detail or "MSDL network error")

            if pdb_outcome == "not_found":
                _record_circuit_success(storage)
                _write_negative_cache(
                    storage, candidate.debug_file, candidate.debug_id, "not_found", neg_ttl
                )
                return _result("not_found", pdb_detail or "Not found on Microsoft Symbol Server")

            if pdb_outcome != "ok" or pdb_path is None:
                _record_circuit_success(storage)
                _write_negative_cache(
                    storage,
                    candidate.debug_file,
                    candidate.debug_id,
                    pdb_outcome,
                    min(neg_ttl, TRANSIENT_MISS_TTL_SECONDS) if neg_ttl else 0,
                )
                return _result("failed", pdb_detail or pdb_outcome)

            _record_circuit_success(storage)

            # Best-effort PE download for x64/arm64 CFI unwind rules (never blocks PDB import).
            pe_path: Path | None = None
            if settings.MS_SYMBOL_FETCH_PE_UNWIND:
                pe_path = await _download_matching_pe(
                    candidate=candidate,
                    pdb_path=pdb_path,
                    symsrv_urls=symsrv_urls,
                    client=client,
                    staging_dir=staging_dir,
                    max_pe_bytes=max_pe_bytes,
                    timeout_budget=timeout_budget,
                    settings=settings,
                    cab_exec_fn=cab_exec_fn,
                    allow_winbindex=allow_winbindex,
                )

            if was_upgrade and pe_path is None:
                clear_negative_cache(storage, candidate.debug_file, candidate.debug_id)
                return _result(
                    "cached_hit",
                    "Already cached locally (PE unwind unavailable)",
                    has_pe_unwind=False,
                )

            # Convert PDB (+ optional PE) into Breakpad .sym via dump_syms.
            pe_used = False
            try:
                sym_bytes, pe_used = await run_dump_syms(
                    pdb_path=pdb_path,
                    pe_path=pe_path,
                    settings=settings,
                    exec_fn=exec_fn,
                )
            except SymbolConversionError as exc:
                if pe_path is None:
                    _write_negative_cache(
                        storage,
                        candidate.debug_file,
                        candidate.debug_id,
                        "conversion_failed",
                        min(neg_ttl, TRANSIENT_MISS_TTL_SECONDS) if neg_ttl else 0,
                    )
                    logger.warning(
                        "dump_syms failed for MSDL PDB %s/%s",
                        candidate.debug_file,
                        candidate.debug_id,
                        exc_info=True,
                    )
                    return _result("failed", str(exc) or "dump_syms conversion failed")
                try:
                    sym_bytes, pe_used = await run_dump_syms(
                        pdb_path=pdb_path,
                        pe_path=None,
                        settings=settings,
                        exec_fn=exec_fn,
                    )
                except SymbolConversionError as exc2:
                    _write_negative_cache(
                        storage,
                        candidate.debug_file,
                        candidate.debug_id,
                        "conversion_failed",
                        min(neg_ttl, TRANSIENT_MISS_TTL_SECONDS) if neg_ttl else 0,
                    )
                    logger.warning(
                        "dump_syms PDB-only fallback failed for %s/%s",
                        candidate.debug_file,
                        candidate.debug_id,
                        exc_info=True,
                    )
                    return _result("failed", str(exc2) or "dump_syms PDB-only fallback failed")

            try:
                header = parse_sym_header(sym_bytes.decode("utf-8", errors="replace"))
            except SymbolConversionError as exc:
                _write_negative_cache(
                    storage,
                    candidate.debug_file,
                    candidate.debug_id,
                    "invalid_sym_header",
                    min(neg_ttl, TRANSIENT_MISS_TTL_SECONDS) if neg_ttl else 0,
                )
                return _result("failed", str(exc) or "Invalid Breakpad .sym header")

            if header.debug_id.upper() != candidate.debug_id.upper():
                logger.warning(
                    "MSDL symbol debug_id mismatch for %s: expected %s got %s",
                    candidate.debug_file,
                    candidate.debug_id,
                    header.debug_id,
                )
                _write_negative_cache(
                    storage,
                    candidate.debug_file,
                    candidate.debug_id,
                    "debug_id_mismatch",
                    min(neg_ttl, TRANSIENT_MISS_TTL_SECONDS) if neg_ttl else 0,
                )
                return _result(
                    "failed",
                    f"debug_id mismatch: expected {candidate.debug_id}, got {header.debug_id}",
                )

            if not _public_cache_has_room(storage, additional_bytes=len(sym_bytes)):
                return _result("skipped", "Public symbol cache quota or host disk floor reached")

            # Preserve requested debug_file casing if header only differs by case
            if (
                header.debug_file.lower() == candidate.debug_file.lower()
                and header.debug_file != candidate.debug_file
                and _is_safe_segment(candidate.debug_file)
            ):
                cand_stem = Path(candidate.debug_file).stem or candidate.debug_file
                if _is_safe_segment(cand_stem):
                    header.debug_file = candidate.debug_file
                    header.stem = cand_stem

            sym_id, db_upgraded = await _persist_public_msdl_symbol(
                session=session,
                session_factory=session_factory,
                db_lock=db_lock,
                storage=storage,
                settings=settings,
                sym_bytes=sym_bytes,
                header=header,
                staged_pdb=pdb_path,
                staged_pe=pe_path if pe_used and header.has_unwind_records else None,
            )
            clear_negative_cache(storage, candidate.debug_file, candidate.debug_id)
            if was_upgrade and not header.has_unwind_records and not db_upgraded:
                return _result(
                    "cached_hit",
                    "Already cached locally (PE unwind unavailable)",
                    has_pe_unwind=False,
                    symbol_id=sym_id,
                )
            is_upgraded = db_upgraded or (was_upgrade and header.has_unwind_records)
            status_label = "upgraded" if is_upgraded else "imported"
            logger.info(
                "%s Windows/public symbol from MSDL: %s/%s (%d bytes, pe_unwind=%s)",
                status_label.capitalize(),
                header.debug_file,
                header.debug_id,
                len(sym_bytes),
                header.has_unwind_records,
            )
            status_detail = (
                "Upgraded with PE unwind records"
                if status_label == "upgraded"
                else "Imported from MSDL"
            )
            return _result(
                status_label,
                status_detail,
                has_pe_unwind=header.has_unwind_records,
                symbol_id=sym_id,
            )


async def _invoke_callback(callback: Any, arg: Any) -> None:
    if callback is None:
        return
    maybe_coro = callback(arg)
    if maybe_coro is not None and asyncio.iscoroutine(maybe_coro):
        await maybe_coro


async def fetch_and_import_windows_symbols(
    candidates: Iterable[WindowsSymbolCandidate],
    storage: StorageManager,
    settings: Settings,
    *,
    project_id: uuid.UUID | str | None = None,
    session: AsyncSession | None = None,
    session_factory: Any = None,
    explicit_targets: bool = False,
    force: bool = False,
    bypass_auto_fetch_flag: bool = False,
    allow_winbindex: bool = False,
    max_concurrency: int = MAX_MS_FETCH_CONCURRENCY,
    on_item_start: Callable[[WindowsSymbolCandidate], Awaitable[None] | None] | None = None,
    on_item_done: Callable[[MSFetchItemResult], Awaitable[None] | None] | None = None,
    exec_fn: Any = None,
    cab_exec_fn: Any = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> MSFetchSummary:
    """Download missing Windows/public symbols from MSDL, convert via dump_syms, and store."""
    summary = MSFetchSummary()
    candidate_list = list(candidates)
    if not bypass_auto_fetch_flag and (
        not settings.ENABLE_PUBLIC_SYMBOLS or not settings.MS_SYMBOL_AUTO_FETCH
    ):
        return summary

    _, symsrv_urls = split_public_symbol_urls(settings.PUBLIC_SYMBOL_URLS)
    if not symsrv_urls:
        if bypass_auto_fetch_flag:
            for cand in candidate_list:
                summary.skipped += 1
                item = MSFetchItemResult(
                    module_name=cand.module_name,
                    debug_file=cand.debug_file,
                    debug_id=cand.debug_id,
                    status="skipped",
                    detail="No SymSrv/MSDL URL configured in PUBLIC_SYMBOL_URLS",
                )
                summary.items.append(item)
                await _invoke_callback(on_item_done, item)
        return summary

    if force:
        # Clear any stale circuit-breaker cooldown before an explicit user retry;
        # consecutive network errors during this batch will still trip it fresh.
        reset_circuit_breaker(storage)
    elif is_circuit_open(storage):
        summary.circuit_open = True
        for cand in candidate_list:
            summary.skipped += 1
            item = MSFetchItemResult(
                module_name=cand.module_name,
                debug_file=cand.debug_file,
                debug_id=cand.debug_id,
                status="skipped",
                detail="MSDL circuit breaker is temporarily open",
            )
            summary.items.append(item)
            await _invoke_callback(on_item_done, item)
        return summary

    if not candidate_list:
        return summary

    sem = asyncio.Semaphore(max(1, int(max_concurrency)))
    db_lock = asyncio.Lock() if (session is not None or session_factory is not None) else None
    client_kwargs: dict[str, Any] = {
        "follow_redirects": True,
        "headers": {"User-Agent": SYMSRV_USER_AGENT},
    }
    if http_transport is not None:
        client_kwargs["transport"] = http_transport

    async with httpx.AsyncClient(**client_kwargs) as client:

        async def _worker(cand: WindowsSymbolCandidate) -> MSFetchItemResult:
            async with sem:
                if is_circuit_open(storage):
                    circuit_item = MSFetchItemResult(
                        module_name=cand.module_name,
                        debug_file=cand.debug_file,
                        debug_id=cand.debug_id,
                        status="skipped",
                        detail="MSDL circuit breaker opened during batch",
                    )
                    await _invoke_callback(on_item_done, circuit_item)
                    return MSFetchItemResult(
                        module_name=cand.module_name,
                        debug_file=cand.debug_file,
                        debug_id=cand.debug_id,
                        status="circuit_open",
                        detail="MSDL circuit breaker opened during batch",
                    )
                await _invoke_callback(on_item_start, cand)
                try:
                    res = await _import_single_candidate(
                        candidate=cand,
                        symsrv_urls=symsrv_urls,
                        client=client,
                        storage=storage,
                        settings=settings,
                        project_id=project_id,
                        exec_fn=exec_fn,
                        cab_exec_fn=cab_exec_fn,
                        session=session,
                        session_factory=session_factory,
                        db_lock=db_lock,
                        explicit_targets=explicit_targets,
                        force=force,
                        allow_winbindex=allow_winbindex,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Unexpected error importing MSDL symbol %s/%s",
                        cand.debug_file,
                        cand.debug_id,
                        exc_info=True,
                    )
                    res = MSFetchItemResult(
                        module_name=cand.module_name,
                        debug_file=cand.debug_file,
                        debug_id=cand.debug_id,
                        status="failed",
                        detail=str(exc) or type(exc).__name__,
                    )
                await _invoke_callback(on_item_done, res)
                return res

        results = await asyncio.gather(*(_worker(c) for c in candidate_list))

    for item in results:
        outcome = item.status
        if outcome == "circuit_open":
            summary.circuit_open = True
            summary.skipped += 1
            summary.items.append(
                MSFetchItemResult(
                    module_name=item.module_name,
                    debug_file=item.debug_file,
                    debug_id=item.debug_id,
                    status="skipped",
                    detail=item.detail,
                )
            )
            continue

        summary.items.append(item)
        if outcome == "imported":
            summary.attempted += 1
            summary.imported += 1
            summary.imported_keys.append(f"{item.debug_file}/{item.debug_id}")
        elif outcome == "upgraded":
            summary.attempted += 1
            summary.upgraded += 1
            summary.imported_keys.append(f"{item.debug_file}/{item.debug_id}")
        elif outcome == "cached_hit":
            summary.cached_hit += 1
        elif outcome == "not_found":
            summary.attempted += 1
            summary.not_found += 1
        elif outcome in {"network_error", "failed"}:
            summary.attempted += 1
            summary.failed += 1
        else:
            summary.skipped += 1

    if is_circuit_open(storage):
        summary.circuit_open = True
    return summary
