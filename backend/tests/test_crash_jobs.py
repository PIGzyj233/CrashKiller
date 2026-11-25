"""Regressions for durable background work across worker and queue failures."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.api.v1.crashes import reprocess_crash
from app.api.v1.symbols import resymbolicate_crashes
from app.config import Settings
from app.db.base import Base
from app.db.models import AIAnalysisRun, CrashJob, CrashReport, Project
from app.db.session import create_async_engine_from_url
from app.schemas.crash import CrashReprocessRequest
from app.schemas.symbol import ResymbolicateRequest
from app.services.events import CrashStatusEvent, InMemoryEventBus
from app.services.storage import StorageManager
from app.workers.jobs import CrashJobClaim
from app.workers.tasks import parse_crash, recover_crash_jobs, run_crash_job
from app.workers.worker import TaskDispatcher
from tests.conftest import make_mock_exec


class FlakyQueue:
    def __init__(self) -> None:
        self.offline = True
        self.enqueued: dict[str, str] = {}

    async def enqueue_job(self, name: str, job_id: str, *, _job_id: str) -> None:
        if self.offline:
            raise ConnectionError("queue unavailable")
        assert name == "run_crash_job"
        self.enqueued.setdefault(_job_id, job_id)


class OfflineNotifications(InMemoryEventBus):
    async def publish(
        self, crash_id: uuid.UUID | str, event: CrashStatusEvent | dict[str, Any],
    ) -> None:
        raise ConnectionError("notifications unavailable")


async def test_reprocess_and_ai_handoff_recover_from_queue_outages(
    tmp_path: Path, test_settings: Settings, storage_manager: StorageManager,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}"
    engine = create_async_engine_from_url(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    settings = test_settings.model_copy(update={"DATABASE_URL": database_url, "LLM_API_KEY": ""})
    queue = FlakyQueue()
    bus = OfflineNotifications()
    dispatcher = TaskDispatcher(settings, storage_manager, bus, factory, arq_pool=queue)
    calls: list[tuple[str, ...]] = []
    output = json.dumps({"threads": [{"frames": [{"module": "game.exe", "function": "Main"}]}]})
    ctx = {
        "settings": settings,
        "session_factory": factory,
        "storage": storage_manager,
        "event_bus": bus,
        "redis": queue,
        "exec_fn": make_mock_exec(stdout=output.encode(), calls_log=calls),
    }

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            project = Project(key="durable-work", name="Test", api_key_hash="placeholder")
            session.add(project)
            await session.flush()
            crash_id = uuid.uuid4()
            raw_dump = tmp_path / "test.dmp"
            raw_dump.write_bytes(b"mock stackwalk input")
            dump_path, _, _ = storage_manager.compress_dump(raw_dump, crash_id)
            session.add(CrashReport(
                id=crash_id, project_id=project.id, status="ANALYZED", app_version="1",
                dump_path=dump_path,
            ))
            await session.commit()
            accepted = await reprocess_crash(
                crash_id=crash_id, payload=CrashReprocessRequest(mode="full"), session=session,
                storage=storage_manager, dispatcher=dispatcher,
            )
        assert accepted.status == "PENDING"
        assert not queue.enqueued

        queue.offline = False
        await recover_crash_jobs(ctx)
        await recover_crash_jobs(ctx)
        parse_key, parse_id = queue.enqueued.popitem()
        assert parse_key == f"crash-job:{parse_id}"
        assert not queue.enqueued

        starting_parse = asyncio.Event()

        async def interrupted_exec(*args: str, **kwargs: Any) -> None:
            starting_parse.set()
            await asyncio.Future()

        interrupted = asyncio.create_task(run_crash_job(
            {**ctx, "exec_fn": interrupted_exec}, parse_id,
        ))
        await asyncio.wait_for(starting_parse.wait(), timeout=2)
        async with factory() as session:
            running_job = await session.get(CrashJob, uuid.UUID(parse_id))
            assert running_job is not None and running_job.lease_id is not None
            stale_claim = CrashJobClaim(
                running_job.id, running_job.lease_id, crash_id, running_job.task,
                running_job.run_ai_override,
            )
        interrupted.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(interrupted, timeout=2)
        await recover_crash_jobs(ctx)
        assert queue.enqueued == {parse_key: parse_id}
        queue.enqueued.clear()

        queue.offline = True
        parsed = await run_crash_job(ctx, parse_id)
        assert parsed is not None and parsed["status"] == "TRIAGING"
        async with factory() as session:
            job = (await session.scalars(select(CrashJob))).one()
            assert job.crash_id == crash_id and job.task == "ai_triage"
            ai_id = str(job.id)
        assert len(calls) == 1
        assert await run_crash_job(ctx, parse_id) is None

        queue.offline = False
        await recover_crash_jobs(ctx)
        assert queue.enqueued == {f"crash-job:{ai_id}": ai_id}
        outcomes = await asyncio.gather(run_crash_job(ctx, ai_id), run_crash_job(ctx, ai_id))
        assert sum(outcome is not None for outcome in outcomes) == 1
        assert (await parse_crash(ctx, crash_id, claim=stale_claim))["status"] == "superseded"
        assert len(calls) == 1
        async with factory() as session:
            crash = await session.get(CrashReport, crash_id)
            assert crash is not None and crash.status == "ANALYZED"
            assert crash.parsed_stacks_json and crash.cluster_id is not None
            assert len((await session.scalars(select(AIAnalysisRun))).all()) == 1
            assert not (await session.scalars(select(CrashJob))).all()
    finally:
        await dispatcher.drain_and_cancel()
        await engine.dispose()


async def test_resymbolicate_missing_reserves_only_eligible_reports(
    async_engine: AsyncEngine, test_settings: Settings, storage_manager: StorageManager,
) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False, autoflush=False)
    dispatcher = TaskDispatcher(
        test_settings, storage_manager, InMemoryEventBus(), factory, arq_pool=FlakyQueue(),
    )
    try:
        async with factory() as session:
            project = Project(key="bounded-queue", name="Test", api_key_hash="placeholder")
            session.add(project)
            await session.flush()
            missing, complete, busy = [
                CrashReport(
                    project_id=project.id, status=report_status, app_version="1",
                    dump_path="available.dmp", missing_symbols_json=symbols,
                )
                for report_status, symbols in [
                    ("ANALYZED", [{"debug_id": "EXPECTED"}]),
                    ("ANALYZED", []),
                    ("TRIAGING", [{"debug_id": "EXPECTED"}]),
                ]
            ]
            session.add_all([missing, complete, busy])
            await session.commit()
            request = ResymbolicateRequest(project_id=project.id, missing_only=True)
            result = await resymbolicate_crashes(request, session, dispatcher, storage_manager)
            assert result.queued_count == 1 and result.crash_ids == [missing.id]
            assert (
                await resymbolicate_crashes(request, session, dispatcher, storage_manager)
            ).queued_count == 0
            jobs = (await session.scalars(select(CrashJob))).all()
            assert [(job.task, job.crash_id) for job in jobs] == [("resymbolicate", missing.id)]
            assert await session.scalar(select(CrashReport.status).where(
                CrashReport.id == complete.id,
            )) == "ANALYZED"
            assert await session.scalar(select(CrashReport.status).where(
                CrashReport.id == busy.id,
            )) == "TRIAGING"
    finally:
        await dispatcher.drain_and_cancel()


async def test_resymbolicate_reports_skipped_active_count(
    async_engine: AsyncEngine, test_settings: Settings, storage_manager: StorageManager,
) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False, autoflush=False)
    dispatcher = TaskDispatcher(
        test_settings, storage_manager, InMemoryEventBus(), factory, arq_pool=FlakyQueue(),
    )
    try:
        async with factory() as session:
            project = Project(key="active-skip", name="Active Skip", api_key_hash="placeholder")
            session.add(project)
            await session.flush()
            idle = CrashReport(
                project_id=project.id,
                status="ANALYZED",
                app_version="1",
                dump_path="idle.dmp",
                missing_symbols_json=[{"debug_id": "EXPECTED"}],
            )
            active = CrashReport(
                project_id=project.id,
                status="PARSING",
                app_version="1",
                dump_path="active.dmp",
                missing_symbols_json=[{"debug_id": "EXPECTED"}],
            )
            session.add_all([idle, active])
            await session.commit()

            request = ResymbolicateRequest(project_id=project.id, missing_only=True)
            first = await resymbolicate_crashes(request, session, dispatcher, storage_manager)
            assert first.queued_count == 1
            assert first.crash_ids == [idle.id]
            assert first.skipped_active_count == 1
            assert first.active_crash_ids == [active.id]

            second = await resymbolicate_crashes(request, session, dispatcher, storage_manager)
            assert second.queued_count == 0
            assert second.skipped_active_count == 2
            assert set(second.active_crash_ids) == {idle.id, active.id}
    finally:
        await dispatcher.drain_and_cancel()
