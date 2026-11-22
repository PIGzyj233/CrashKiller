"""ARQ async background tasks for symbol ingestion, crash parsing, AI triage, and GC."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.db.models import CrashReport
from app.db.session import get_session_factory
from app.services import clustering, dump_parser, settings_service, symbol_service
from app.services.ai_agent import AITriageAgent
from app.services.events import EventBus, get_event_bus
from app.services.storage import StorageManager

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
) -> dict[str, Any]:
    """Parse a compressed minidump, cluster stack frames, and trigger AI triage if needed."""
    session_factory, base_settings, event_bus = _resolve_ctx_defaults(ctx)
    cid = uuid.UUID(str(crash_id))

    try:
        should_dispatch_ai = False
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

            crash.status = "PARSING"
            await session.commit()
            await session.refresh(crash)
            if project is not None:
                await session.refresh(project)
            await event_bus.publish_status(
                cid,
                "PARSING",
                stage="parsing_dump",
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

            if should_run_ai:
                crash.status = "TRIAGING"
                ai_verdict_snapshot = crash.ai_verdict
                await session.commit()
                should_dispatch_ai = True
            else:
                crash.status = "ANALYZED"
                ai_verdict_snapshot = crash.ai_verdict
                await session.commit()

        if should_dispatch_ai:
            await event_bus.publish_status(
                cid,
                "TRIAGING",
                stage="ai_triage",
                ai_verdict=ai_verdict_snapshot,
                error=None,
            )
            await _dispatch_ai_triage(ctx, cid)
            return {
                "crash_id": str(cid),
                "status": "TRIAGING",
                "queued_ai": True,
            }

        await event_bus.publish_status(
            cid,
            "ANALYZED",
            stage="completed",
            ai_verdict=ai_verdict_snapshot,
            error=None,
        )
        return {
            "crash_id": str(cid),
            "status": "ANALYZED",
            "queued_ai": False,
        }

    except Exception as exc:
        err_msg = str(exc) or type(exc).__name__
        logger.exception("parse_crash failed for %s: %s", cid, err_msg)
        try:
            async with session_factory() as err_session:
                crash = await err_session.get(CrashReport, cid)
                if crash is not None:
                    crash.status = "FAILED"
                    await err_session.commit()
        except Exception:
            logger.exception("Failed to persist FAILED status for crash %s", cid)
        await event_bus.publish_status(
            cid,
            "FAILED",
            stage="failed",
            ai_verdict=None,
            error=err_msg,
        )
        return {
            "crash_id": str(cid),
            "status": "FAILED",
            "error": err_msg,
        }


async def ai_triage(
    ctx: dict[str, Any],
    crash_id: str | uuid.UUID,
) -> dict[str, Any]:
    """Run the ReAct AI triage agent on a parsed CrashReport."""
    session_factory, base_settings, event_bus = _resolve_ctx_defaults(ctx)
    cid = uuid.UUID(str(crash_id))

    try:
        async with session_factory() as session:
            crash = await session.get(CrashReport, cid)
            if crash is None:
                return {"crash_id": str(cid), "status": "not_found"}

            if crash.status != "TRIAGING":
                ai_verdict_snapshot = crash.ai_verdict
                crash.status = "TRIAGING"
                await session.commit()
                await session.refresh(crash)
                await event_bus.publish_status(
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
            )
            await session.refresh(crash)
            final_status = crash.status
            final_verdict = crash.ai_verdict
            run_id_str = str(run.id)
            await event_bus.publish_status(
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
    except Exception as exc:
        err_msg = str(exc) or type(exc).__name__
        logger.exception("ai_triage failed for %s: %s", cid, err_msg)
        try:
            async with session_factory() as err_session:
                crash = await err_session.get(CrashReport, cid)
                if crash is not None:
                    crash.status = "FAILED"
                    await err_session.commit()
        except Exception:
            logger.exception("Failed to persist FAILED status during ai_triage for %s", cid)
        await event_bus.publish_status(
            cid,
            "FAILED",
            stage="failed",
            ai_verdict=None,
            error=err_msg,
        )
        return {
            "crash_id": str(cid),
            "status": "FAILED",
            "error": err_msg,
        }


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

    session_factory, base_settings, event_bus = _resolve_ctx_defaults(ctx)
    processed_ids: list[str] = []
    candidate_ids: list[uuid.UUID] = []

    async with session_factory() as discovery_session:
        stmt = select(CrashReport.id, CrashReport.missing_symbols_json).where(
            CrashReport.dump_path.is_not(None)
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
        should_dispatch_ai = False
        ai_verdict_snapshot: str | None = None
        try:
            async with session_factory() as crash_session:
                try:
                    stmt = (
                        select(CrashReport)
                        .options(selectinload(CrashReport.project))
                        .where(CrashReport.id == cid)
                    )
                    crash = (await crash_session.execute(stmt)).scalar_one_or_none()
                    if crash is None or not crash.dump_path:
                        continue

                    effective = await settings_service.get_effective_settings(
                        crash_session, base_settings=base_settings
                    )
                    eff_settings = effective.to_app_settings(base_settings=base_settings)
                    eff_storage = _effective_storage(ctx, eff_settings)

                    parsed = await dump_parser.parse_dump(
                        crash.dump_path,
                        eff_storage,
                        eff_settings,
                        exec_fn=ctx.get("exec_fn"),
                        project_id=crash.project_id,
                    )
                    crash.crash_reason = parsed.crash_reason
                    crash.crash_address = parsed.crash_address
                    if parsed.os_version and not crash.os_version:
                        crash.os_version = parsed.os_version
                    crash.parsed_summary_json = parsed.parsed_summary_json
                    crash.parsed_stacks_json = parsed.parsed_stacks_json
                    crash.missing_symbols_json = parsed.missing_symbols_json

                    proj_cfg = settings_service.resolve_project_settings(
                        effective,
                        crash.project.settings_json if crash.project else None,
                    )
                    cluster_result = await clustering.cluster_crash(
                        crash_session,
                        crash,
                        crash.project,
                        triage_mode=proj_cfg["ai_triage_mode"],
                        system_settings=effective.model_dump(),
                    )

                    if cluster_result.should_run_ai:
                        crash.status = "TRIAGING"
                        ai_verdict_snapshot = crash.ai_verdict
                        await crash_session.commit()
                        should_dispatch_ai = True
                    else:
                        crash.status = "ANALYZED"
                        ai_verdict_snapshot = crash.ai_verdict
                        await crash_session.commit()
                except Exception:
                    await crash_session.rollback()
                    raise

            if should_dispatch_ai:
                await event_bus.publish_status(
                    cid,
                    "TRIAGING",
                    stage="resymbolicate_ai",
                    ai_verdict=ai_verdict_snapshot,
                    error=None,
                )
                await _dispatch_ai_triage(ctx, cid)
            else:
                await event_bus.publish_status(
                    cid,
                    "ANALYZED",
                    stage="resymbolicate_completed",
                    ai_verdict=ai_verdict_snapshot,
                    error=None,
                )
            processed_ids.append(str(cid))
        except Exception as exc:
            err_msg = str(exc) or type(exc).__name__
            logger.exception("resymbolicate failed for %s: %s", cid, err_msg)
            try:
                async with session_factory() as err_session:
                    failed_crash = await err_session.get(CrashReport, cid)
                    if failed_crash is not None:
                        failed_crash.status = "FAILED"
                        await err_session.commit()
            except Exception:
                logger.exception(
                    "Failed to persist FAILED status during resymbolicate for %s", cid
                )
            await event_bus.publish_status(
                cid,
                "FAILED",
                stage="failed",
                ai_verdict=None,
                error=err_msg,
            )

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
