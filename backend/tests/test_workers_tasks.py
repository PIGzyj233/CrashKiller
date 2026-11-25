"""Unit tests for WorkerSettings, crash pipeline, resymbolication, GC, and dispatcher."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import CrashReport, Project, SystemSetting
from app.services.events import (
    InMemoryEventBus,
)
from app.services.storage import StorageManager
from app.workers import (
    TASK_REGISTRY,
    TaskDispatcher,
    WorkerSettings,
    ai_triage,
    parse_crash,
    parse_redis_settings,
    resymbolicate,
    symbol_ingest,
)
from app.workers.tasks import disk_gc, fetch_ms_symbols, recover_crash_jobs, run_crash_job
from tests._worker_test_helpers import _sample_stackwalk_json
from tests.conftest import make_disk_usage_fn, make_mock_exec


def test_worker_settings_contract() -> None:
    assert WorkerSettings.functions[:-2] == [
        symbol_ingest,
        parse_crash,
        ai_triage,
        resymbolicate,
        disk_gc,
    ]
    assert WorkerSettings.functions[-2].coroutine is fetch_ms_symbols
    assert WorkerSettings.functions[-2].keep_result_s == 0
    assert WorkerSettings.functions[-1].coroutine is run_crash_job
    assert WorkerSettings.functions[-1].keep_result_s == 0
    assert {job.coroutine for job in WorkerSettings.cron_jobs} == {disk_gc, recover_crash_jobs}
    assert set(TASK_REGISTRY.keys()) == {
        "symbol_ingest",
        "parse_crash",
        "ai_triage",
        "resymbolicate",
        "fetch_ms_symbols",
        "disk_gc",
        "run_crash_job",
    }
    rs = parse_redis_settings("memory://")
    assert rs.host == "localhost"


async def test_parse_crash_pipeline_smart_triage_and_corrupt_dump_handling(
    async_engine: AsyncEngine,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    session_factory = async_sessionmaker(
        bind=async_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    bus = InMemoryEventBus()

    async with session_factory() as session:
        proj = Project(
            key="wk-proj",
            name="Worker Project",
            api_key_hash=hashlib.sha256(b"k").hexdigest(),
            settings_json={},
        )
        session.add(proj)
        await session.flush()

        # Prepare real compressed minidump file.
        raw_dmp = tmp_path / "c1.dmp"
        raw_dmp.write_bytes(b"MDMP_TEST_BYTES")
        c1_id = uuid.uuid4()
        rel_dump, raw_sz, comp_sz = storage_manager.compress_dump(raw_dmp, c1_id)

        crash1 = CrashReport(
            id=c1_id,
            project_id=proj.id,
            status="PENDING",
            app_version="1.0.0",
            dump_path=rel_dump,
            dump_raw_size=raw_sz,
            dump_compressed_size=comp_sz,
        )
        session.add(crash1)
        await session.commit()

    llm_calls = 0

    class MockLLM:
        def __init__(self) -> None:
            async def _create(**kw: Any) -> Any:
                nonlocal llm_calls
                llm_calls += 1
                has_tool = any(
                    isinstance(m, dict) and m.get("role") == "tool"
                    for m in (kw.get("messages") or [])
                )
                if not has_tool:
                    return SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                message=SimpleNamespace(
                                    content=None,
                                    tool_calls=[
                                        SimpleNamespace(
                                            id="ov_1",
                                            type="function",
                                            function=SimpleNamespace(
                                                name="get_crash_overview",
                                                arguments="{}",
                                            ),
                                        )
                                    ],
                                )
                            )
                        ],
                        usage=SimpleNamespace(prompt_tokens=30, completion_tokens=10),
                    )
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps(
                                    {
                                        "verdict": "NEW_ISSUE",
                                        "confidence": 0.9,
                                        "suggested_title": "App CrashFunc crash",
                                        "root_cause_analysis": "Null deref",
                                    }
                                ),
                                tool_calls=None,
                            )
                        )
                    ],
                    usage=SimpleNamespace(prompt_tokens=50, completion_tokens=20),
                )

            self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))

    ctx = {
        "settings": test_settings,
        "storage": storage_manager,
        "session_factory": session_factory,
        "event_bus": bus,
        "exec_fn": make_mock_exec(stdout=_sample_stackwalk_json()),
        "openai_client": MockLLM(),
    }

    async with bus.subscribe(c1_id) as sub:
        res1 = await parse_crash(ctx, str(c1_id))
        assert res1["queued_ai"] is True
        events_1 = [
            await sub.get(timeout=1.0),
            await sub.get(timeout=1.0),
            await sub.get(timeout=1.0),
        ]
    assert [e["status"] for e in events_1] == ["PARSING", "TRIAGING", "ANALYZED"]
    assert llm_calls == 2
    assert list(storage_manager.partition_path("staging").iterdir()) == []

    # Second identical crash skips AI in SMART_TRIAGE (0 additional LLM calls).
    async with session_factory() as session:
        c2_id = uuid.uuid4()
        raw_dmp2 = tmp_path / "c2.dmp"
        raw_dmp2.write_bytes(b"MDMP_TEST_BYTES_2")
        rel_dump2, rsz2, csz2 = storage_manager.compress_dump(raw_dmp2, c2_id)
        session.add(
            CrashReport(
                id=c2_id,
                project_id=proj.id,
                status="PENDING",
                app_version="1.0.1",
                dump_path=rel_dump2,
                dump_raw_size=rsz2,
                dump_compressed_size=csz2,
            )
        )
        await session.commit()

    res2 = await parse_crash(ctx, str(c2_id))
    assert res2["queued_ai"] is False
    assert res2["status"] == "ANALYZED"
    assert llm_calls == 2

    # Corrupt .dmp.zst triggers zstd.ZstdError -> CrashReport.status == "FAILED" + SSE error.
    async with session_factory() as session:
        bad_id = uuid.uuid4()
        bad_rel = f"dumps/{bad_id}/{bad_id}.dmp.zst"
        storage_manager.atomic_write_bytes(bad_rel, b"NOT_A_VALID_ZSTD_FRAME")
        session.add(
            CrashReport(
                id=bad_id,
                project_id=proj.id,
                status="PENDING",
                app_version="1.0.0",
                dump_path=bad_rel,
            )
        )
        await session.commit()

    async with bus.subscribe(bad_id) as bad_sub:
        bad_res = await parse_crash(ctx, str(bad_id))
        assert bad_res["status"] == "FAILED"
        bad_ev1 = await bad_sub.get(timeout=1.0)
        bad_ev2 = await bad_sub.get(timeout=1.0)
    assert bad_ev1["status"] == "PARSING"
    assert bad_ev2["status"] == "FAILED"
    assert bad_ev2["error"]
    assert list(storage_manager.partition_path("staging").iterdir()) == []
    await bus.close()


async def test_resymbolicate_symbol_ingest_disk_gc_and_dispatcher(
    async_engine: AsyncEngine,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    session_factory = async_sessionmaker(
        bind=async_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    bus = InMemoryEventBus()

    async with session_factory() as session:
        proj = Project(
            key="resym-proj",
            name="Resym Project",
            api_key_hash=hashlib.sha256(b"k").hexdigest(),
            settings_json={"ai_triage_mode": "MANUAL"},
        )
        session.add(proj)
        await session.flush()

        # 1. symbol_ingest cleans up staged files.
        staged_pdb = storage_manager.partition_path("staging") / "app.pdb"
        staged_pdb.write_bytes(b"FAKE_PDB")
        sym_bytes = b"MODULE windows x86_64 111122223333444455556666777788881 App.pdb\n"

        ctx = {
            "settings": test_settings,
            "storage": storage_manager,
            "session_factory": session_factory,
            "event_bus": bus,
            "exec_fn": make_mock_exec(stdout=sym_bytes),
        }
        sym_res = await symbol_ingest(
            ctx,
            project_id=str(proj.id),
            app_version="1.0.0",
            pdb_staged_path=str(staged_pdb),
            pdb_filename="App.pdb",
        )
        assert sym_res["debug_file"] == "App.pdb"
        assert not staged_pdb.exists()

        # 2. resymbolicate updates stack & cluster after missing symbols are resolved.
        c_id = uuid.uuid4()
        raw_dmp = tmp_path / "resym.dmp"
        raw_dmp.write_bytes(b"MDMP")
        rel_dump, rsz, csz = storage_manager.compress_dump(raw_dmp, c_id)
        crash = CrashReport(
            id=c_id,
            project_id=proj.id,
            status="PENDING",
            app_version="1.0.0",
            dump_path=rel_dump,
            dump_raw_size=rsz,
            dump_compressed_size=csz,
        )
        session.add(crash)
        await session.commit()

    ctx_unresolved = {
        "settings": test_settings,
        "storage": storage_manager,
        "session_factory": session_factory,
        "event_bus": bus,
        "exec_fn": make_mock_exec(stdout=_sample_stackwalk_json(None, missing=True)),
    }
    await parse_crash(ctx_unresolved, str(c_id))

    async with session_factory() as session:
        c_before = await session.get(CrashReport, c_id)
        assert c_before is not None
        old_cluster_id = c_before.cluster_id
        assert len(c_before.missing_symbols_json or []) == 1

    ctx_resolved = {
        "settings": test_settings,
        "storage": storage_manager,
        "session_factory": session_factory,
        "event_bus": bus,
        "exec_fn": make_mock_exec(stdout=_sample_stackwalk_json("App::Resolved()", missing=False)),
    }
    resym_out = await resymbolicate(
        ctx_resolved,
        project_id=str(proj.id),
        debug_id="AAAABBBBCCCCDDDD11112222333344441",
    )
    assert resym_out["resymbolicated_count"] == 1

    async with session_factory() as session:
        c_after = await session.get(CrashReport, c_id)
        assert c_after is not None
        assert c_after.cluster_id != old_cluster_id
        assert c_after.missing_symbols_json == []

        # 3. disk_gc respects dynamic DB overrides.
        storage_floor = StorageManager(
            storage_manager.data_dir,
            test_settings,
            disk_usage_fn=make_disk_usage_fn(free_gb=20.0),
        )
        session.add(SystemSetting(key="host_safety_floor_gb", value_json={"value": 30.0}))
        await session.commit()

    gc_res = await disk_gc(
        {
            "settings": test_settings,
            "storage": storage_floor,
            "session_factory": session_factory,
            "event_bus": bus,
        }
    )
    assert gc_res["emergency_triggered"] is True

    # 4. TaskDispatcher in memory:// tracks & drains tasks; in redis:// uses ArqRedis.
    mem_settings = test_settings.model_copy(update={"REDIS_URL": "memory://"})
    mem_dispatcher = TaskDispatcher(
        settings=mem_settings,
        storage=storage_manager,
        event_bus=bus,
        session_factory=session_factory,
    )
    await mem_dispatcher.enqueue("disk_gc")
    await mem_dispatcher.drain_and_cancel(timeout=2.0)
    assert mem_dispatcher.pending_count == 0

    enqueued_jobs: list[tuple[str, tuple[Any, ...]]] = []

    class FakeArqPool:
        async def enqueue_job(self, name: str, *args: Any, **_kwargs: Any) -> SimpleNamespace:
            enqueued_jobs.append((name, args))
            return SimpleNamespace(job_id="job-123")

    redis_settings = test_settings.model_copy(update={"REDIS_URL": "redis://localhost:6379/0"})
    redis_dispatcher = TaskDispatcher(
        settings=redis_settings,
        storage=storage_manager,
        event_bus=bus,
        arq_pool=FakeArqPool(),  # type: ignore[arg-type]
    )
    job_id = await redis_dispatcher.enqueue("parse_crash", str(c_id))
    assert job_id == "job-123"
    assert enqueued_jobs == [("parse_crash", (str(c_id),))]
    await bus.close()
