"""Regression tests for Phase 1 & Phase 2 architecture governance improvements."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    CrashCluster,
    CrashMissingSymbol,
    CrashReport,
    Issue,
    Project,
    StoragePartitionUsage,
)
from app.services import clustering
from app.services.events import RedisEventBus, crash_channel
from app.services.storage import StorageManager
from app.workers import (
    COMPUTE_QUEUE_NAME,
    IO_QUEUE_NAME,
    ComputeWorkerSettings,
    IOWorkerSettings,
    TaskDispatcher,
)
from app.workers.jobs import enqueue_crash_job, queue_for_crash_task, queue_for_worker_task
from tests._api_integration_helpers import _drain, open_api_harness


@pytest.fixture
async def api_harness(tmp_path: Path):
    async with open_api_harness(tmp_path) as harness:
        yield harness


async def test_dashboard_uses_ledger_without_full_disk_scan_after_init(
    api_harness: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = api_harness.client
    proj = (
        await client.post(
            "/api/v1/projects",
            json={
                "key": "ledger-dash",
                "name": "Ledger Dash",
                "settings_json": {"ai_triage_mode": "MANUAL"},
            },
        )
    ).json()

    up = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj["api_key"]},
        data={"project_key": proj["key"], "app_version": "1.0.0"},
        files=[
            ("dump_file", ("sample.dmp", b"MDMP_LEDGER_TEST", "application/octet-stream")),
            ("log_files", ("app.log", b"2026-11-28 10:00:00.000 INFO hello\n", "text/plain")),
        ],
    )
    assert up.status_code == 202
    await _drain(api_harness)

    # Verify ledger rows are populated and non-zero for dumps and logs.
    async with api_harness.app.state.session_factory() as session:
        rows = {
            r.partition: r
            for r in (await session.scalars(select(StoragePartitionUsage))).all()
        }
        assert rows["dumps"].used_bytes > 0
        assert rows["dumps"].file_count == 1
        assert rows["logs"].used_bytes > 0
        assert rows["logs"].file_count == 2
        assert rows["staging"].used_bytes == 0

    # Once ledger is initialized, Dashboard must NOT invoke partition_used_bytes() disk scans.
    scan_calls = 0
    orig_scan = StorageManager.partition_used_bytes

    def _counting_scan(self: StorageManager, partition: str) -> tuple[int, int]:
        nonlocal scan_calls
        scan_calls += 1
        return orig_scan(self, partition)

    monkeypatch.setattr(StorageManager, "partition_used_bytes", _counting_scan)

    dash = await client.get("/api/v1/dashboard", params={"project_id": proj["id"]})
    assert dash.status_code == 200
    body = dash.json()
    assert scan_calls == 0
    assert body["kpis"]["total_crashes"] == 1
    assert body["kpis"]["analyzed_crashes"] == 1
    assert body["storage"]["partitions"]["dumps"]["used_bytes"] > 0
    assert body["storage"]["partitions"]["logs"]["used_bytes"] > 0


async def test_concurrent_uploads_staging_log_409_and_async_symbol_upload(
    api_harness: Any,
) -> None:
    client = api_harness.client
    proj = (
        await client.post(
            "/api/v1/projects",
            json={
                "key": "async-ingest",
                "name": "Async Ingest",
                "settings_json": {"ai_triage_mode": "MANUAL"},
            },
        )
    ).json()

    # Hold worker execution briefly so we can inspect PENDING staged state cleanly.
    release_exec = asyncio.Event()
    orig_exec = api_harness.app.state.dispatcher.exec_fn

    async def _gated_exec(*args: str, **kwargs: Any) -> Any:
        await release_exec.wait()
        return await orig_exec(*args, **kwargs)

    api_harness.app.state.dispatcher.exec_fn = _gated_exec

    # Pause memory dispatcher slots so crash remains PENDING before Stage 0 runs.
    acquired: list[bool] = []
    for _ in range(api_harness.settings.COMPUTE_WORKER_MAX_JOBS):
        await api_harness.app.state.dispatcher._memory_compute_slots.acquire()
        acquired.append(True)

    try:
        up = await client.post(
            "/api/v1/crashes/upload",
            headers={"X-API-Key": proj["api_key"]},
            data={"project_key": proj["key"], "app_version": "2.0.0"},
            files=[
                ("dump_file", ("staged.dmp", b"MDMP_STAGED_BYTES", "application/octet-stream")),
                (
                    "log_files",
                    ("staged.log", b"2026-11-28 10:00:00.000 ERROR staged boom\n", "text/plain"),
                ),
            ],
        )
        assert up.status_code == 202
        crash_id = up.json()["crash_id"]
        log_id = up.json()["log_ids"][0]

        # While still PENDING/staged:
        # 1. Log lines endpoint returns 409 Conflict (indexing in progress, NOT 410 GC evicted).
        log_resp = await client.get(f"/api/v1/logs/{log_id}/lines")
        assert log_resp.status_code == 409

        # 2. Dump artifact download still streams raw staged dump bytes seamlessly.
        dmp_resp = await client.get(f"/api/v1/crashes/{crash_id}/artifacts/dump")
        assert dmp_resp.status_code == 200
        assert dmp_resp.content == b"MDMP_STAGED_BYTES"

        # 3. Emergency GC must NOT delete staged upload files belonging to the active crash.
        gc_resp = await client.post("/api/v1/settings/gc", json={"force_emergency": True})
        assert gc_resp.status_code == 200
        assert (api_harness.data_dir / "staging" / "uploads" / crash_id / "dump.raw").is_file()
    finally:
        for _ in acquired:
            api_harness.app.state.dispatcher._memory_compute_slots.release()
        release_exec.set()

    await _drain(api_harness)

    # After worker finishes Stage 0 + parse: log is readable (200), staging is empty,
    # and missing symbols are indexed in crash_missing_symbols.
    log_ok = await client.get(f"/api/v1/logs/{log_id}/lines")
    assert log_ok.status_code == 200
    assert "staged boom" in log_ok.json()["lines"][0]["text"]
    assert not list((api_harness.data_dir / "staging").rglob("*"))

    async with api_harness.app.state.session_factory() as session:
        crash_row = await session.get(CrashReport, uuid.UUID(crash_id))
        assert crash_row is not None
        assert crash_row.status == "ANALYZED"
        assert crash_row.missing_symbols_indexed is True
        missing_rows = (
            await session.scalars(
                select(CrashMissingSymbol).where(CrashMissingSymbol.crash_id == crash_row.id)
            )
        ).all()
        assert len(missing_rows) >= 1

    # Concurrent crash uploads succeed without global lock contention.
    uploads = await asyncio.gather(
        *[
            client.post(
                "/api/v1/crashes/upload",
                headers={"X-API-Key": proj["api_key"]},
                data={"project_key": proj["key"], "app_version": "2.0.0"},
                files={"dump_file": (f"c{i}.dmp", f"MDMP_CONCURRENT_{i}".encode())},
            )
            for i in range(6)
        ]
    )
    assert all(r.status_code == 202 for r in uploads)
    await _drain(api_harness)
    assert not list((api_harness.data_dir / "staging").rglob("*"))

    # Async symbol upload returns 202 and completes in background worker.
    sym_async = await client.post(
        "/api/v1/symbols/upload",
        headers={"X-API-Key": proj["api_key"]},
        data={"project_key": proj["key"], "app_version": "2.0.0", "async_mode": "true"},
        files={"pdb_file": ("AsyncMod.pdb", b"VALID_PDB")},
    )
    assert sym_async.status_code == 202
    assert sym_async.json()["status"] == "PENDING"
    assert sym_async.json()["job_id"]
    await _drain(api_harness)

    listed_syms = (await client.get("/api/v1/symbols", params={"project_id": proj["id"]})).json()
    assert listed_syms["total"] == 1
    assert not list((api_harness.data_dir / "staging").rglob("*"))


async def test_clustering_skips_recall_on_known_issue_and_pg_trgm_op_compiles(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proj = Project(key="skip-recall", name="Skip Recall", api_key_hash="h")
    db_session.add(proj)
    await db_session.flush()

    issue = Issue(project_id=proj.id, title="Known GPU Crash", status="KNOWN")
    db_session.add(issue)
    await db_session.flush()

    stacks = {
        "crashing_thread_index": 0,
        "threads": [
            {
                "thread_index": 0,
                "frames": [{"module": "Renderer.dll", "function": "Renderer::Draw()"}],
            }
        ],
        "modules": [],
    }
    first = CrashReport(
        project_id=proj.id,
        status="PARSING",
        app_version="1.0.0",
        crash_reason="EXCEPTION_ACCESS_VIOLATION",
        parsed_stacks_json=stacks,
    )
    db_session.add(first)
    await db_session.flush()

    r1 = await clustering.cluster_crash(db_session, first, proj, triage_mode="SMART_TRIAGE")
    assert r1.should_run_ai is True
    r1.cluster.issue_id = issue.id
    await db_session.flush()

    # Second identical crash resolves to known unambiguous issue -> must NOT run recall.
    recall_calls = 0

    async def _fail_if_called(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        nonlocal recall_calls
        recall_calls += 1
        return []

    monkeypatch.setattr("app.services.clustering.engine.recall_similar_issues", _fail_if_called)

    second = CrashReport(
        project_id=proj.id,
        status="PARSING",
        app_version="1.0.0",
        crash_reason="EXCEPTION_ACCESS_VIOLATION",
        parsed_stacks_json=stacks,
    )
    db_session.add(second)
    await db_session.flush()

    r2 = await clustering.cluster_crash(db_session, second, proj, triage_mode="SMART_TRIAGE")
    assert r2.should_run_ai is False
    assert r2.similar_issues == []
    assert recall_calls == 0

    from sqlalchemy.dialects.postgresql import asyncpg as pg_asyncpg

    # Verify PostgreSQL % operator queries compile cleanly for GIN trigram index usage.
    cluster_stmt = select(
        CrashCluster.id,
        func.similarity(CrashCluster.signature_text, "renderer.dll!Renderer::Draw()"),
    ).where(CrashCluster.signature_text.op("%")("renderer.dll!Renderer::Draw()"))
    issue_stmt = select(Issue.id).where(
        or_(Issue.title.op("%")("GPU crash"), Issue.root_cause.op("%")("GPU crash"))
    )
    compiled_c = str(cluster_stmt.compile(dialect=pg_asyncpg.dialect()))
    compiled_i = str(issue_stmt.compile(dialect=pg_asyncpg.dialect()))
    assert " % " in compiled_c
    assert " % " in compiled_i


async def test_split_worker_queues_routing_and_io_compute_isolation(
    tmp_path: Path,
    test_settings: Settings,
    storage_manager: StorageManager,
) -> None:
    assert ComputeWorkerSettings.queue_name == COMPUTE_QUEUE_NAME
    assert IOWorkerSettings.queue_name == IO_QUEUE_NAME
    # Default combined-worker routing keeps all tasks on COMPUTE_QUEUE_NAME.
    assert queue_for_crash_task("parse_crash") == COMPUTE_QUEUE_NAME
    assert queue_for_crash_task("ai_triage") == COMPUTE_QUEUE_NAME
    assert queue_for_worker_task("fetch_ms_symbols") == COMPUTE_QUEUE_NAME
    # Opt-in split routing sends IO-bound tasks to IO_QUEUE_NAME.
    assert queue_for_crash_task("parse_crash", split_queues=True) == COMPUTE_QUEUE_NAME
    assert queue_for_crash_task("resymbolicate", split_queues=True) == COMPUTE_QUEUE_NAME
    assert queue_for_crash_task("ai_triage", split_queues=True) == IO_QUEUE_NAME
    assert queue_for_worker_task("fetch_ms_symbols", split_queues=True) == IO_QUEUE_NAME
    assert queue_for_worker_task("symbol_ingest", split_queues=True) == COMPUTE_QUEUE_NAME

    recorded: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    class RecordingArqPool:
        async def enqueue_job(self, name: str, *args: Any, **kwargs: Any) -> Any:
            recorded.append((name, args, kwargs))
            return type("Job", (), {"job_id": kwargs.get("_job_id", "j")})()

    pool = RecordingArqPool()
    parse_jid = uuid.uuid4()
    ai_jid = uuid.uuid4()
    await enqueue_crash_job(pool, parse_jid, task="parse_crash")  # type: ignore[arg-type]
    await enqueue_crash_job(pool, ai_jid, task="ai_triage")  # type: ignore[arg-type]
    await enqueue_crash_job(
        pool, ai_jid, task="ai_triage", split_queues=True  # type: ignore[arg-type]
    )
    await enqueue_crash_job(
        pool, parse_jid, task="resymbolicate", split_queues=True  # type: ignore[arg-type]
    )
    assert "_queue_name" not in recorded[0][2]
    assert "_queue_name" not in recorded[1][2]
    assert recorded[2][2]["_queue_name"] == IO_QUEUE_NAME
    # Worker-to-worker enqueues (e.g. IOWorker -> resymbolicate) must pass COMPUTE explicitly.
    assert recorded[3][2]["_queue_name"] == COMPUTE_QUEUE_NAME

    # In memory:// mode, saturating all IO slots must NOT block compute tasks.
    settings = test_settings.model_copy(
        update={
            "REDIS_URL": "memory://",
            "COMPUTE_WORKER_MAX_JOBS": 2,
            "IO_WORKER_MAX_JOBS": 1,
        }
    )
    from app.services.events import InMemoryEventBus

    dispatcher = TaskDispatcher(settings, storage_manager, InMemoryEventBus())
    try:
        # Hold the sole IO slot.
        await dispatcher._memory_io_slots.acquire()
        compute_done = asyncio.Event()

        async def _compute_work() -> None:
            async with dispatcher._memory_compute_slots:
                compute_done.set()

        t = asyncio.create_task(_compute_work())
        await asyncio.wait_for(compute_done.wait(), timeout=1.0)
        await t
    finally:
        dispatcher._memory_io_slots.release()
        await dispatcher.drain_and_cancel()


async def test_redis_event_bus_multiplexes_many_subscribers_on_single_pubsub() -> None:
    pubsub_creations = 0
    subscribed_channels: list[str] = []
    unsubscribed_channels: list[str] = []
    closed_count = 0
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    class MultiplexFakePubSub:
        async def subscribe(self, channel: str) -> None:
            subscribed_channels.append(channel)

        async def unsubscribe(self, channel: str) -> None:
            unsubscribed_channels.append(channel)

        async def get_message(self, **kwargs: Any) -> dict[str, Any] | None:
            timeout = float(kwargs.get("timeout") or 0.05)
            try:
                return await asyncio.wait_for(incoming.get(), timeout=timeout)
            except TimeoutError:
                return None

        async def aclose(self) -> None:
            nonlocal closed_count
            closed_count += 1

    class FakeRedis:
        def pubsub(self) -> MultiplexFakePubSub:
            nonlocal pubsub_creations
            pubsub_creations += 1
            return MultiplexFakePubSub()

        async def publish(self, channel: str, payload: str) -> int:
            await incoming.put({"type": "message", "channel": channel, "data": payload})
            return 1

    bus = RedisEventBus("redis://localhost:6379/0", redis_client=FakeRedis(), poll_interval=0.05)
    cids = [uuid.uuid4() for _ in range(5)]
    subs = []
    try:
        # 2 subscribers per crash_id -> 10 concurrent subscriptions across 5 channels
        for cid in cids:
            s1 = bus.subscribe(cid)
            s2 = bus.subscribe(cid)
            await s1.__aenter__()
            await s2.__aenter__()
            subs.extend([(cid, s1), (cid, s2)])

        # Only ONE underlying Redis PubSub connection created and 5 unique channel subscribes.
        assert pubsub_creations == 1
        assert len(subscribed_channels) == 5
        assert set(subscribed_channels) == {crash_channel(c) for c in cids}

        for cid in cids:
            await bus.publish_status(cid, "ANALYZED", stage="done")

        for cid, sub in subs:
            evt = await sub.get(timeout=1.0)
            assert evt["crash_id"] == str(cid)
            assert evt["status"] == "ANALYZED"
    finally:
        for _, sub in subs:
            await sub.close()

    assert len(unsubscribed_channels) == 5
    assert closed_count == 1
    await bus.close()


async def test_gc_claim_prevents_reprocess_race_on_evicted_dump(
    db_session: AsyncSession,
    tmp_data_dir: Path,
    test_settings: Settings,
) -> None:
    from fastapi import HTTPException

    from app.api.v1.crashes import reprocess_crash
    from app.schemas.crash import CrashReprocessRequest
    from app.services.events import InMemoryEventBus
    from tests.conftest import make_disk_usage_fn

    mgr = StorageManager(
        data_dir=tmp_data_dir,
        settings=test_settings,
        disk_usage_fn=make_disk_usage_fn(free_gb=40.0),
    )
    proj = Project(key="gc-race", name="GC Race", api_key_hash="h")
    db_session.add(proj)
    await db_session.flush()

    cid = uuid.uuid4()
    rel = f"dumps/{cid}/{cid}.dmp.zst"
    mgr.atomic_write_bytes(rel, b"DUMP_BYTES")
    crash = CrashReport(
        id=cid,
        project_id=proj.id,
        status="ANALYZED",
        app_version="1.0.0",
        dump_path=rel,
    )
    db_session.add(crash)
    await db_session.commit()

    # Emergency GC atomically clears dump_path and unlinks file.
    gc_report = await mgr.run_gc(db_session, force_emergency=True)
    assert gc_report["deleted_files_count"] >= 1
    assert not mgr.resolve_path(rel).exists()

    # Reprocess full must reject cleanly with 409 Conflict (no job staged).
    dispatcher = TaskDispatcher(test_settings, mgr, InMemoryEventBus())
    try:
        with pytest.raises(HTTPException) as exc_info:
            await reprocess_crash(
                crash_id=cid,
                payload=CrashReprocessRequest(mode="full"),
                session=db_session,
                storage=mgr,
                dispatcher=dispatcher,
            )
        assert exc_info.value.status_code == 409
    finally:
        await dispatcher.drain_and_cancel()

