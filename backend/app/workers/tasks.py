"""ARQ async background tasks for symbol ingestion, crash parsing, AI triage, and GC."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.db.models import CrashJob, CrashReport
from app.db.session import get_session_factory
from app.services import clustering, dump_parser, settings_service, symbol_service
from app.services.ai_agent import AITriageAgent
from app.services.events import EventBus, get_event_bus
from app.services.storage import StorageManager
from app.workers.jobs import (
    ACTIVE_CRASH_STATUSES,
    CRASH_JOB_TIMEOUT_SECONDS,
    JOB_DELIVERY_TIMEOUT_SECONDS,
    CrashJobClaim,
    CrashJobSuperseded,
    claim_crash_job,
    enqueue_crash_job,
    finish_crash_job,
    lock_current_crash_job,
    pending_crash_jobs,
    release_crash_job,
    stage_crash_job,
)

logger = logging.getLogger(__name__)


def _resolve_ctx_defaults(ctx: dict[str, Any]) -> tuple[Any, Settings, EventBus]:
    session_factory = ctx.get("session_factory") or get_session_factory()
    base_settings: Settings = ctx.get("settings") or get_settings()
    event_bus: EventBus = ctx.get("event_bus") or get_event_bus(base_settings)
    return session_factory, base_settings, event_bus


def _effective_storage(ctx: dict[str, Any], effective_settings: Settings) -> StorageManager:
    injected: StorageManager | None = ctx.get("storage")
    if injected is not None:
        return StorageManager(
            data_dir=injected.data_dir,
            settings=effective_settings,
            disk_usage_fn=injected.disk_usage_fn,
        )
    return StorageManager(data_dir=effective_settings.DATA_DIR, settings=effective_settings)


async def _dispatch_ai_triage(ctx: dict[str, Any], crash_id: uuid.UUID) -> None:
    dispatcher = ctx.get("dispatcher")
    if dispatcher is not None:
        await dispatcher.enqueue("ai_triage", str(crash_id))
        return
    redis_pool = ctx.get("redis")
    if redis_pool is not None:
        await redis_pool.enqueue_job("ai_triage", str(crash_id))
        return
    await ai_triage(ctx, str(crash_id))


async def _publish_status(
    event_bus: EventBus, crash_id: uuid.UUID, status: str, **info: Any,
) -> None:
    try:
        async with asyncio.timeout(JOB_DELIVERY_TIMEOUT_SECONDS):
            await event_bus.publish_status(crash_id, status, **info)
    except Exception:
        logger.warning("Could not publish crash status %s for %s", status, crash_id, exc_info=True)


async def _deliver_crash_job(ctx: dict[str, Any], job_id: uuid.UUID) -> None:
    try:
        if (dispatcher := ctx.get("dispatcher")) is not None:
            await dispatcher.kick_crash_jobs([job_id])
        elif (redis_pool := ctx.get("redis")) is not None:
            async with asyncio.timeout(JOB_DELIVERY_TIMEOUT_SECONDS):
                await enqueue_crash_job(redis_pool, job_id)
    except Exception:
        logger.warning("Crash job %s remains pending after delivery failure", job_id, exc_info=True)


async def _set_crash_stage(
    session: AsyncSession,
    crash: CrashReport,
    status: str,
    claim: CrashJobClaim | None,
) -> None:
    if claim is not None:
        await session.refresh(crash, attribute_names=["status"], with_for_update=True)
        await lock_current_crash_job(session, claim)
    crash.status = status
    await session.commit()


async def _fail_crash(
    session_factory: Any,
    event_bus: EventBus,
    crash_id: uuid.UUID,
    message: str,
    claim: CrashJobClaim | None,
) -> dict[str, Any]:
    try:
        async with session_factory() as session:
            crash = await session.get(CrashReport, crash_id, with_for_update=claim is not None)
            if crash is not None:
                crash.status = "FAILED"
                if claim is not None:
                    await finish_crash_job(session, claim)
                await session.commit()
    except CrashJobSuperseded:
        return {"crash_id": str(crash_id), "status": "superseded"}
    await _publish_status(event_bus, crash_id, "FAILED", stage="failed", error=message)
    return {"crash_id": str(crash_id), "status": "FAILED", "error": message}


async def recover_crash_jobs(ctx: dict[str, Any]) -> None:
    """Deliver outstanding jobs even when the API or Redis failed during the first enqueue."""
    async with ctx["session_factory"]() as session:
        job_ids = await pending_crash_jobs(session)
    try:
        async with asyncio.timeout(JOB_DELIVERY_TIMEOUT_SECONDS):
            for job_id in job_ids:
                await enqueue_crash_job(ctx["redis"], job_id)
    except Exception:
        logger.warning("Could not recover pending crash jobs", exc_info=True)


async def run_crash_job(ctx: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    """Claim a single crash operation and relinquish the claim if work is interrupted."""
    session_factory, _, event_bus = _resolve_ctx_defaults(ctx)
    async with session_factory() as session:
        claim = await claim_crash_job(session, uuid.UUID(job_id))
    if claim is None:
        return None

    try:
        # Leave time for a terminal database update before the queue's hard timeout.
        async with asyncio.timeout(CRASH_JOB_TIMEOUT_SECONDS - 10):
            if claim.task == "ai_triage":
                return await ai_triage(ctx, claim.crash_id, claim=claim)
            return await parse_crash(
                ctx, claim.crash_id, claim.run_ai_override,
                claim=claim, resymbolication=claim.task == "resymbolicate",
            )
    except TimeoutError:
        return await _fail_crash(
            session_factory, event_bus, claim.crash_id, "Crash processing timed out", claim,
        )
    finally:
        try:
            async with session_factory() as session:
                await release_crash_job(session, claim)
        except Exception:
            logger.warning("Could not release crash job %s", claim.job_id, exc_info=True)


async def symbol_ingest(
    ctx: dict[str, Any],
    project_id: str | uuid.UUID,
    app_version: str,
    pdb_staged_path: str | None = None,
    pdb_filename: str | None = None,
    pe_staged_path: str | None = None,
    pe_filename: str | None = None,
) -> dict[str, Any]:
    """Convert staged PDB/PE binaries into Breakpad .sym and persist metadata."""
    session_factory, base_settings, _ = _resolve_ctx_defaults(ctx)
    pid = uuid.UUID(str(project_id))
    pdb_path = Path(pdb_staged_path) if pdb_staged_path else None
    pe_path = Path(pe_staged_path) if pe_staged_path else None

    try:
        async with session_factory() as session:
            effective_app = await settings_service.get_effective_app_settings(
                session, base_settings=base_settings
            )
            eff_storage = _effective_storage(ctx, effective_app)
            result = await symbol_service.ingest_symbol(
                session=session,
                storage=eff_storage,
                settings=effective_app,
                project_id=pid,
                app_version=app_version,
                pdb_filename=pdb_filename,
                pdb_bytes_or_path=pdb_path,
                pe_filename=pe_filename,
                pe_bytes_or_path=pe_path,
                exec_fn=ctx.get("exec_fn"),
            )
            await session.refresh(result.symbol)
            return {
                "symbol_id": str(result.symbol.id),
                "debug_file": result.symbol.debug_file,
                "debug_id": result.symbol.debug_id,
                "has_pe_unwind": result.symbol.has_pe_unwind,
                "deduplicated": result.deduplicated,
                "upgraded": result.upgraded,
            }
    finally:
        for path in (pdb_path, pe_path):
            if path is not None:
                path.unlink(missing_ok=True)


async def parse_crash(
    ctx: dict[str, Any],
    crash_id: str | uuid.UUID,
    run_ai_override: bool | None = None,
    *,
    claim: CrashJobClaim | None = None,
    resymbolication: bool = False,
) -> dict[str, Any]:
    """Parse a compressed minidump, cluster stack frames, and trigger AI triage if needed."""
    session_factory, base_settings, event_bus = _resolve_ctx_defaults(ctx)
    cid = uuid.UUID(str(crash_id))

    try:
        next_job_id: uuid.UUID | None = None
        ai_verdict_snapshot: str | None = None
        async with session_factory() as session:
            stmt = (
                select(CrashReport)
                .options(selectinload(CrashReport.project))
                .where(CrashReport.id == cid)
            )
            crash = (await session.execute(stmt)).scalar_one_or_none()
            if crash is None:
                return {"crash_id": str(cid), "status": "not_found"}
            if claim is None and ctx.get("redis") is not None:
                # Ignore messages left in Redis from before outstanding jobs were persisted.
                has_durable_job = await session.scalar(
                    select(CrashJob.id).where(CrashJob.crash_id == cid)
                )
                if has_durable_job or (
                    not resymbolication and crash.status not in {"PENDING", "PARSING"}
                ):
                    return {"crash_id": str(cid), "status": "superseded"}

            project = crash.project
            dump_path = crash.dump_path
            existing_os_version = crash.os_version
            project_settings_json = project.settings_json if project else None
            ai_verdict_snapshot = crash.ai_verdict

            effective = await settings_service.get_effective_settings(
                session, base_settings=base_settings
            )
            eff_settings = effective.to_app_settings(base_settings=base_settings)
            eff_storage = _effective_storage(ctx, eff_settings)

            if not resymbolication or claim is not None:
                await _set_crash_stage(session, crash, "PARSING", claim)
                await session.refresh(crash)
                if project is not None:
                    await session.refresh(project)
                await _publish_status(
                    event_bus,
                    cid,
                    "PARSING",
                    stage="resymbolicate" if resymbolication else "parsing_dump",
                    ai_verdict=ai_verdict_snapshot,
                    error=None,
                )

            if not dump_path:
                raise ValueError(f"CrashReport {cid} has no dump_path")

            # dump_parser.parse_dump owns staging directory creation and cleanup.
            parsed = await dump_parser.parse_dump(
                dump_path,
                eff_storage,
                eff_settings,
                exec_fn=ctx.get("exec_fn"),
                project_id=crash.project_id,
            )

            crash.crash_reason = parsed.crash_reason
            crash.crash_address = parsed.crash_address
            if parsed.os_version and not existing_os_version:
                crash.os_version = parsed.os_version
            crash.parsed_summary_json = parsed.parsed_summary_json
            crash.parsed_stacks_json = parsed.parsed_stacks_json
            crash.missing_symbols_json = parsed.missing_symbols_json

            proj_cfg = settings_service.resolve_project_settings(
                effective,
                project_settings_json,
            )
            cluster_result = await clustering.cluster_crash(
                session,
                crash,
                project,
                triage_mode=proj_cfg["ai_triage_mode"],
                system_settings=effective.model_dump(),
            )

            should_run_ai = (
                bool(run_ai_override)
                if run_ai_override is not None
                else cluster_result.should_run_ai
            )

            crash.status = "TRIAGING" if should_run_ai else "ANALYZED"
            ai_verdict_snapshot = crash.ai_verdict
            if claim is not None:
                await finish_crash_job(session, claim)
            if should_run_ai and (claim is not None or ctx.get("redis") is not None):
                next_job_id = stage_crash_job(session, cid, "ai_triage")
            await session.commit()

        if should_run_ai:
            await _publish_status(
                event_bus,
                cid,
                "TRIAGING",
                stage="resymbolicate_ai" if resymbolication else "ai_triage",
                ai_verdict=ai_verdict_snapshot,
                error=None,
            )
            if next_job_id is not None:
                await _deliver_crash_job(ctx, next_job_id)
            else:
                await _dispatch_ai_triage(ctx, cid)
            return {
                "crash_id": str(cid),
                "status": "TRIAGING",
                "queued_ai": True,
            }

        await _publish_status(
            event_bus,
            cid,
            "ANALYZED",
            stage="resymbolicate_completed" if resymbolication else "completed",
            ai_verdict=ai_verdict_snapshot,
            error=None,
        )
        return {
            "crash_id": str(cid),
            "status": "ANALYZED",
            "queued_ai": False,
        }

    except CrashJobSuperseded:
        return {"crash_id": str(cid), "status": "superseded"}
    except Exception as exc:
        err_msg = str(exc) or type(exc).__name__
        logger.exception("parse_crash failed for %s: %s", cid, err_msg)
        return await _fail_crash(session_factory, event_bus, cid, err_msg, claim)


async def ai_triage(
    ctx: dict[str, Any],
    crash_id: str | uuid.UUID,
    *,
    claim: CrashJobClaim | None = None,
) -> dict[str, Any]:
    """Run the ReAct AI triage agent on a parsed CrashReport."""
    session_factory, base_settings, event_bus = _resolve_ctx_defaults(ctx)
    cid = uuid.UUID(str(crash_id))

    try:
        async with session_factory() as session:
            crash = await session.get(CrashReport, cid)
            if crash is None:
                return {"crash_id": str(cid), "status": "not_found"}
            if claim is None and ctx.get("redis") is not None:
                has_durable_job = await session.scalar(
                    select(CrashJob.id).where(CrashJob.crash_id == cid)
                )
                if has_durable_job or crash.status != "TRIAGING":
                    return {"crash_id": str(cid), "status": "superseded"}

            notify_status = crash.status != "TRIAGING"
            ai_verdict_snapshot = crash.ai_verdict
            if notify_status or claim is not None:
                await _set_crash_stage(session, crash, "TRIAGING", claim)
                await session.refresh(crash)
            if notify_status:
                await _publish_status(
                    event_bus,
                    cid,
                    "TRIAGING",
                    stage="ai_triage",
                    ai_verdict=ai_verdict_snapshot,
                    error=None,
                )

            effective_app = await settings_service.get_effective_app_settings(
                session, base_settings=base_settings
            )
            eff_storage = _effective_storage(ctx, effective_app)
            agent = AITriageAgent(storage=eff_storage, settings=effective_app)
            run = await agent.run_triage(
                session,
                crash,
                openai_client=ctx.get("openai_client"),
                commit=claim is None,
            )
            run_id_str = str(run.id)
            if claim is not None:
                await finish_crash_job(session, claim)
                await session.commit()
            await session.refresh(crash)
            final_status = crash.status
            final_verdict = crash.ai_verdict
        await _publish_status(
            event_bus,
            cid,
            final_status,
            stage="completed",
            ai_verdict=final_verdict,
            error=None,
        )
        return {
            "crash_id": str(cid),
            "status": final_status,
            "ai_verdict": final_verdict,
            "run_id": run_id_str,
        }
    except CrashJobSuperseded:
        return {"crash_id": str(cid), "status": "superseded"}
    except Exception as exc:
        err_msg = str(exc) or type(exc).__name__
        logger.exception("ai_triage failed for %s: %s", cid, err_msg)
        return await _fail_crash(session_factory, event_bus, cid, err_msg, claim)


async def resymbolicate(
    ctx: dict[str, Any],
    crash_ids: list[str] | None = None,
    project_id: str | uuid.UUID | None = None,
    debug_id: str | None = None,
) -> dict[str, Any]:
    """Re-run minidump-stackwalk and re-cluster crashes after new symbols are uploaded."""
    if crash_ids is not None and len(crash_ids) == 0:
        return {
            "resymbolicated_count": 0,
            "crash_ids": [],
        }

    session_factory, _, _ = _resolve_ctx_defaults(ctx)
    processed_ids: list[str] = []
    candidate_ids: list[uuid.UUID] = []

    async with session_factory() as discovery_session:
        stmt = select(CrashReport.id, CrashReport.missing_symbols_json).where(
            CrashReport.dump_path.is_not(None),
            CrashReport.status.notin_(ACTIVE_CRASH_STATUSES),
        )
        if crash_ids is not None:
            stmt = stmt.where(CrashReport.id.in_([uuid.UUID(str(x)) for x in crash_ids]))
        if project_id is not None:
            stmt = stmt.where(CrashReport.project_id == uuid.UUID(str(project_id)))

        rows = (await discovery_session.execute(stmt)).all()
        target_debug_id = debug_id.strip().upper() if debug_id else None
        for row_id, missing_json in rows:
            if target_debug_id is not None and not any(
                str(entry.get("debug_id") or "").strip().upper() == target_debug_id
                for entry in (missing_json or [])
                if isinstance(entry, dict)
            ):
                continue
            candidate_ids.append(row_id)

    for cid in candidate_ids:
        result = await parse_crash(ctx, cid, resymbolication=True)
        if result["status"] in {"ANALYZED", "TRIAGING"}:
            processed_ids.append(str(cid))

    return {
        "resymbolicated_count": len(processed_ids),
        "crash_ids": processed_ids,
    }


async def disk_gc(
    ctx: dict[str, Any],
    force_emergency: bool = False,
) -> dict[str, Any]:
    """Run storage retention & quota garbage collection using dynamic settings."""
    session_factory, base_settings, _ = _resolve_ctx_defaults(ctx)
    async with session_factory() as session:
        effective_app = await settings_service.get_effective_app_settings(
            session, base_settings=base_settings
        )
        eff_storage = _effective_storage(ctx, effective_app)
        return await eff_storage.run_gc(session, force_emergency=force_emergency)
