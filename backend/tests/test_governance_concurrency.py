"""Concurrency, quota, lease, cancellation, and reconnect regressions for Phase 1 & Phase 2."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import CrashLog, CrashReport, StoragePartitionUsage, Symbol
from app.services.crash_ingest import materialize_staged_crash_artifacts
from app.services.events import InMemoryEventBus, RedisEventBus
from app.services.missing_symbols import backfill_unindexed_missing_symbols
from app.services.storage import StorageManager
from app.workers import COMPUTE_QUEUE_NAME
from app.workers import tasks as worker_tasks
from app.workers.jobs import stage_crash_job
from app.workers.worker import ACTIVE_MEMORY_SYMBOL_JOBS
from tests._api_integration_helpers import _drain, open_api_harness
from tests.conftest import make_disk_usage_fn


@pytest.fixture
async def api_harness(tmp_path: Path):
    async with open_api_harness(tmp_path) as harness:
        yield harness


async def test_worker_finalization_enforces_dump_quota_and_cleans_artifacts(
    api_harness: Any,
) -> None:
    client = api_harness.client
    proj = (
        await client.post(
            "/api/v1/projects",
            json={
                "key": "dump-quota-worker",
                "name": "Dump Quota Worker",
                "settings_json": {"ai_triage_mode": "MANUAL"},
            },
        )
    ).json()

    # 32-byte dumps quota: passes 1-byte API headroom check, must fail at worker finalization.
    upd = await client.put(
        "/api/v1/settings",
        json={"dumps_quota_gb": 32 / (1024**3)},
    )
    assert upd.status_code == 200

    up = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj["api_key"]},
        data={"project_key": proj["key"], "app_version": "1.0.0"},
        files={"dump_file": ("incompressible.dmp", bytes(range(256)))},
    )
    assert up.status_code == 202
    crash_id = uuid.UUID(up.json()["crash_id"])
    await _drain(api_harness)

    async with api_harness.app.state.session_factory() as session:
        crash = await session.get(CrashReport, crash_id)
        assert crash is not None
        assert crash.status == "FAILED"
        assert crash.dump_path is None
        rows = {
            r.partition: r
            for r in (await session.scalars(select(StoragePartitionUsage))).all()
        }
        assert rows["dumps"].used_bytes == 0
        assert rows["staging"].used_bytes == 0

    assert not list((api_harness.data_dir / "dumps").rglob("*.zst"))
    assert not list((api_harness.data_dir / "staging").rglob("*.raw"))


async def test_staging_leases_protect_uncommitted_crash_and_queued_async_symbol_from_gc(
    db_session: AsyncSession,
    tmp_data_dir: Path,
    test_settings: Settings,
) -> None:
    mgr = StorageManager(
        data_dir=tmp_data_dir,
        settings=test_settings,
        disk_usage_fn=make_disk_usage_fn(free_gb=40.0),
    )

    # 1. Active crash upload lease protects raw dump even before DB row commits.
    cid = uuid.uuid4()
    crash_raw = mgr.atomic_write_bytes(f"staging/uploads/{cid}/dump.raw", b"UNCOMMITTED_DUMP")
    with mgr.staging_upload_lease("upload", str(cid)):
        await mgr.run_gc(db_session, force_emergency=True)
        assert crash_raw.is_file()

    # Once lease is released and no DB row exists, emergency GC reclaims the orphan.
    await mgr.run_gc(db_session, force_emergency=True)
    assert not crash_raw.exists()

    # 2. Active symbol queue lease protects queued async symbol upload from emergency GC.
    uid = uuid.uuid4().hex
    sym_rel_dir = f"staging/symbol_uploads/{uid}"
    sym_staged = mgr.atomic_write_bytes(f"{sym_rel_dir}/Queued.pdb", b"QUEUED_PDB")
    lease_path = mgr.write_staging_queue_lease(sym_rel_dir, ttl_seconds=600.0)
    await mgr.run_gc(db_session, force_emergency=True)
    assert sym_staged.is_file()

    # Once queue lease expires, emergency GC reclaims it.
    lease_path.write_text(json.dumps({"expires_at": 1.0}), encoding="utf-8")
    await mgr.run_gc(db_session, force_emergency=True)
    assert not sym_staged.exists()


async def test_evicted_log_returns_410_even_during_reprocessing(
    api_harness: Any,
) -> None:
    client = api_harness.client
    proj = (
        await client.post(
            "/api/v1/projects",
            json={"key": "evicted-reproc", "name": "Evicted Reproc"},
        )
    ).json()

    cid = uuid.uuid4()
    lid = uuid.uuid4()
    async with api_harness.app.state.session_factory() as session:
        session.add(
            CrashReport(
                id=cid,
                project_id=uuid.UUID(proj["id"]),
                status="PENDING",
                app_version="1.0.0",
            )
        )
        await session.flush()
        session.add(
            CrashLog(
                id=lid,
                crash_id=cid,
                filename="evicted.log",
                zst_path=None,
                idx_path=None,
                staged_raw_path=None,
            )
        )
        await session.commit()

    resp = await client.get(f"/api/v1/logs/{lid}/lines")
    assert resp.status_code == 410


async def test_missing_symbols_preserves_code_file_code_id_pair_and_readonly_get(
    api_harness: Any,
) -> None:
    client = api_harness.client
    proj = (
        await client.post(
            "/api/v1/projects",
            json={"key": "pair-ms", "name": "Pair MS"},
        )
    ).json()
    pid = uuid.UUID(proj["id"])
    now = datetime.now(UTC)
    c_old = uuid.uuid4()
    c_new = uuid.uuid4()

    async with api_harness.app.state.session_factory() as session:
        session.add_all(
            [
                CrashReport(
                    id=c_old,
                    project_id=pid,
                    status="ANALYZED",
                    app_version="1.0.0",
                    crash_time=now - timedelta(hours=2),
                    missing_symbols_indexed=False,
                    missing_symbols_json=[
                        {
                            "module_name": "old.exe",
                            "debug_file": "Shared.pdb",
                            "debug_id": "AAA111BBB222",
                            "code_file": "z_old.exe",
                            "code_id": "11111111",
                        }
                    ],
                ),
                CrashReport(
                    id=c_new,
                    project_id=pid,
                    status="ANALYZED",
                    app_version="1.0.0",
                    crash_time=now,
                    missing_symbols_indexed=False,
                    missing_symbols_json=[
                        {
                            "module_name": "new.dll",
                            "debug_file": "Shared.pdb",
                            "debug_id": "AAA111BBB222",
                            "code_file": "a_new.dll",
                            "code_id": "99999999",
                        }
                    ],
                ),
            ]
        )
        await session.commit()

    # 1. GET before backfill must stay read-only and keep paired (code_file, code_id).
    r1 = await client.get("/api/v1/symbols/missing", params={"project_id": proj["id"]})
    assert r1.status_code == 200
    items1 = r1.json()
    assert len(items1) == 1
    assert items1[0]["code_file"] == "a_new.dll"
    assert items1[0]["code_id"] == "99999999"

    async with api_harness.app.state.session_factory() as session:
        old_row = await session.get(CrashReport, c_old)
        assert old_row is not None and old_row.missing_symbols_indexed is False
        indexed_cnt = await backfill_unindexed_missing_symbols(
            session, project_id=pid, commit_per_batch=True
        )
        assert indexed_cnt == 2

    # 2. GET after committed backfill takes SQL fast path and still preserves the pair.
    r2 = await client.get("/api/v1/symbols/missing", params={"project_id": proj["id"]})
    assert r2.status_code == 200
    items2 = r2.json()
    assert len(items2) == 1
    assert items2[0]["code_file"] == "a_new.dll"
    assert items2[0]["code_id"] == "99999999"


async def test_redis_event_bus_retries_reconnect_and_cleans_cancelled_setup_subscriber() -> None:
    # Part 1: transient Redis outage where first reconnect fails and second succeeds.
    pubsub_attempts = 0
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    class FlakyPubSub:
        def __init__(self, fail_once: bool) -> None:
            self._fail_once = fail_once

        async def subscribe(self, channel: str) -> None:
            _ = channel

        async def unsubscribe(self, channel: str) -> None:
            _ = channel

        async def get_message(self, **kwargs: Any) -> dict[str, Any] | None:
            if self._fail_once:
                self._fail_once = False
                raise ConnectionError("redis dropped")
            timeout = float(kwargs.get("timeout") or 0.05)
            try:
                return await asyncio.wait_for(incoming.get(), timeout=timeout)
            except TimeoutError:
                return None

        async def aclose(self) -> None:
            return None

    class ReconnectingRedis:
        def pubsub(self) -> FlakyPubSub:
            nonlocal pubsub_attempts
            pubsub_attempts += 1
            if pubsub_attempts == 2:
                raise ConnectionError("redis still down")
            return FlakyPubSub(fail_once=(pubsub_attempts == 1))

        async def publish(self, channel: str, payload: str) -> int:
            await incoming.put({"type": "message", "channel": channel, "data": payload})
            return 1

    cid = uuid.uuid4()
    bus = RedisEventBus(
        "redis://localhost:6379/0",
        redis_client=ReconnectingRedis(),
        poll_interval=0.02,
    )
    try:
        async with bus.subscribe(cid) as sub:
            for _ in range(100):
                if pubsub_attempts >= 3:
                    break
                await asyncio.sleep(0.02)
            assert pubsub_attempts >= 3
            await bus.publish_status(cid, "ANALYZED", stage="recovered")
            evt = await sub.get(timeout=1.0)
            assert evt["status"] == "ANALYZED"
            assert evt["stage"] == "recovered"
    finally:
        await bus.close()

    # Part 2: cancelling a concurrent second subscriber while awaiting initial subscribe.
    sub_entered = asyncio.Event()
    release_sub = asyncio.Event()
    closed_count = 0

    class SlowSubscribePubSub:
        async def subscribe(self, channel: str) -> None:
            _ = channel
            sub_entered.set()
            await release_sub.wait()

        async def unsubscribe(self, channel: str) -> None:
            _ = channel

        async def get_message(self, **kwargs: Any) -> dict[str, Any] | None:
            await asyncio.sleep(float(kwargs.get("timeout") or 0.02))
            return None

        async def aclose(self) -> None:
            nonlocal closed_count
            closed_count += 1

    class SlowRedis:
        def pubsub(self) -> SlowSubscribePubSub:
            return SlowSubscribePubSub()

        async def publish(self, channel: str, payload: str) -> int:
            _ = (channel, payload)
            return 0

    bus2 = RedisEventBus("redis://localhost:6379/0", redis_client=SlowRedis(), poll_interval=0.02)
    cid2 = uuid.uuid4()

    async def _enter(sub_obj: Any) -> None:
        async with sub_obj:
            await asyncio.sleep(0.05)

    try:
        t1 = asyncio.create_task(_enter(bus2.subscribe(cid2)))
        await asyncio.wait_for(sub_entered.wait(), timeout=1.0)
        t2 = asyncio.create_task(_enter(bus2.subscribe(cid2)))
        await asyncio.sleep(0.02)
        t2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t2
        release_sub.set()
        await asyncio.wait_for(t1, timeout=1.0)
        assert not bus2._active_subscriptions
        assert not bus2._channel_subscribers
        assert closed_count == 1
    finally:
        await bus2.close()


async def test_run_crash_job_timeout_cleans_staged_files_and_log_staged_path(
    api_harness: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = api_harness.client
    proj = (
        await client.post(
            "/api/v1/projects",
            json={"key": "timeout-clean", "name": "Timeout Clean"},
        )
    ).json()
    pid = uuid.UUID(proj["id"])
    cid = uuid.uuid4()
    lid = uuid.uuid4()
    mgr: StorageManager = api_harness.app.state.storage
    dump_rel = f"staging/uploads/{cid}/dump.raw"
    log_rel = f"staging/uploads/{cid}/log_{lid}.raw"

    async with api_harness.app.state.session_factory() as session:
        await mgr.commit_usage_deltas(
            session,
            {"staging": 31},
            {"staging": 2},
            enforce_quota=False,
        )
        session.add(
            CrashReport(
                id=cid,
                project_id=pid,
                status="PENDING",
                app_version="1.0.0",
                dump_path=dump_rel,
            )
        )
        await session.flush()
        session.add(
            CrashLog(
                id=lid,
                crash_id=cid,
                filename="timeout.log",
                staged_raw_path=log_rel,
            )
        )
        jid = stage_crash_job(session, cid, "parse_crash")
        await session.commit()

    mgr.atomic_write_bytes(dump_rel, b"RAW_DUMP_TIMEOUT")
    mgr.atomic_write_bytes(log_rel, b"RAW_LOG_TIMEOUT")

    async def _timeout_parse(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise TimeoutError("simulated timeout")

    monkeypatch.setattr(worker_tasks, "parse_crash", _timeout_parse)

    ctx = {
        "settings": api_harness.settings,
        "session_factory": api_harness.app.state.session_factory,
        "storage": mgr,
        "event_bus": InMemoryEventBus(),
    }
    res = await worker_tasks.run_crash_job(ctx, str(jid))
    assert res is not None and res["status"] == "FAILED"

    assert not (api_harness.data_dir / "staging" / "uploads" / str(cid)).exists()
    async with api_harness.app.state.session_factory() as session:
        crash = await session.get(CrashReport, cid)
        log_row = await session.get(CrashLog, lid)
        assert crash is not None and crash.status == "FAILED" and crash.dump_path is None
        assert log_row is not None and log_row.staged_raw_path is None
        staging_row = await session.get(StoragePartitionUsage, "staging")
        assert staging_row is not None and staging_row.used_bytes == 0


async def test_redis_event_bus_serializes_concurrent_first_subscriptions() -> None:
    in_flight = 0
    max_in_flight = 0
    pubsub_creations = 0
    subscribed: set[str] = set()
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    class SerialCheckPubSub:
        async def subscribe(self, channel: str) -> None:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.005)
                subscribed.add(channel)
            finally:
                in_flight -= 1

        async def unsubscribe(self, channel: str) -> None:
            subscribed.discard(channel)

        async def get_message(self, **kwargs: Any) -> dict[str, Any] | None:
            timeout = float(kwargs.get("timeout") or 0.02)
            try:
                return await asyncio.wait_for(incoming.get(), timeout=timeout)
            except TimeoutError:
                return None

        async def aclose(self) -> None:
            return None

    class FakeRedis:
        def pubsub(self) -> SerialCheckPubSub:
            nonlocal pubsub_creations
            pubsub_creations += 1
            return SerialCheckPubSub()

        async def publish(self, channel: str, payload: str) -> int:
            if channel in subscribed:
                await incoming.put({"type": "message", "channel": channel, "data": payload})
                return 1
            return 0

    bus = RedisEventBus("redis://localhost:6379/0", redis_client=FakeRedis(), poll_interval=0.02)
    cids = [uuid.uuid4() for _ in range(15)]
    subs = [bus.subscribe(cid) for cid in cids]
    try:
        await asyncio.gather(*(s.__aenter__() for s in subs))
        assert pubsub_creations == 1
        assert max_in_flight == 1
        assert len(subscribed) == len(cids)

        for cid in cids:
            await bus.publish_status(cid, "ANALYZED", stage="ok")
        for cid, sub in zip(cids, subs, strict=True):
            evt = await sub.get(timeout=1.0)
            assert evt["crash_id"] == str(cid)
            assert evt["status"] == "ANALYZED"
    finally:
        await asyncio.gather(*(s.close() for s in subs))
        await bus.close()


async def test_delete_symbol_initializes_ledger_before_unlinking_and_scratch_excluded(
    api_harness: Any,
) -> None:
    client = api_harness.client
    proj = (
        await client.post(
            "/api/v1/projects",
            json={"key": "del-ledger", "name": "Del Ledger"},
        )
    ).json()
    pid = uuid.UUID(proj["id"])
    mgr: StorageManager = api_harness.app.state.storage

    # Pre-populate two 100-byte symbols on disk with an EMPTY storage_partition_usage ledger.
    rel1 = f"symbols/sym/{pid}/a.pdb/AAA1/a.sym"
    rel2 = f"symbols/sym/{pid}/b.pdb/BBB2/b.sym"
    mgr.atomic_write_bytes(rel1, b"A" * 100)
    mgr.atomic_write_bytes(rel2, b"B" * 100)
    sym1_id = uuid.uuid4()
    sym2_id = uuid.uuid4()

    async with api_harness.app.state.session_factory() as session:
        session.add_all(
            [
                Symbol(
                    id=sym1_id,
                    project_id=pid,
                    app_version="1.0",
                    module_name="a.dll",
                    debug_file="a.pdb",
                    debug_id="AAA1",
                    sym_path=rel1,
                    sym_size=100,
                ),
                Symbol(
                    id=sym2_id,
                    project_id=pid,
                    app_version="1.0",
                    module_name="b.dll",
                    debug_file="b.pdb",
                    debug_id="BBB2",
                    sym_path=rel2,
                    sym_size=100,
                ),
            ]
        )
        await session.commit()

    resp = await client.delete(f"/api/v1/symbols/{sym1_id}")
    assert resp.status_code == 204

    async with api_harness.app.state.session_factory() as session:
        sym_row = await session.get(StoragePartitionUsage, "symbols/sym")
        assert sym_row is not None
        assert sym_row.used_bytes == 100
        assert sym_row.file_count == 1

        # Active scratch workspace bytes must NOT be persisted into storage_partition_usage.
        with mgr.staging_workspace("dump") as ws:
            (ws / "scratch.dmp").write_bytes(b"X" * 512)
            async with mgr.storage_io_lock(exclusive=True):
                await mgr.reconcile_usage(session)
                await session.commit()
            staging_row = await session.get(StoragePartitionUsage, "staging")
            assert staging_row is not None and staging_row.used_bytes == 0
            # Live report still reflects active scratch bytes while workspace exists.
            live_report = await mgr.get_disk_usage_report_async(session)
            assert live_report["partitions"]["staging"]["used_bytes"] == 512

        # Once scratch workspace exits, report returns to 0 with zero phantom bytes.
        after_report = await mgr.get_disk_usage_report_async(session)
        assert after_report["partitions"]["staging"]["used_bytes"] == 0


async def test_materialize_cancellation_waits_for_thread_and_cleans_orphan_dump(
    api_harness: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = api_harness.client
    proj = (
        await client.post(
            "/api/v1/projects",
            json={"key": "cancel-mat", "name": "Cancel Mat"},
        )
    ).json()
    pid = uuid.UUID(proj["id"])
    cid = uuid.uuid4()
    mgr: StorageManager = api_harness.app.state.storage
    dump_rel = f"staging/uploads/{cid}/dump.raw"
    mgr.atomic_write_bytes(dump_rel, b"MDMP_CANCEL_TEST")

    async with api_harness.app.state.session_factory() as session:
        session.add(
            CrashReport(
                id=cid,
                project_id=pid,
                status="PARSING",
                app_version="1.0.0",
                dump_path=dump_rel,
            )
        )
        await session.commit()

    entered = threading.Event()
    orig_compress = StorageManager.compress_dump

    def _slow_compress(
        self: StorageManager,
        src: Any,
        crash_id_arg: Any,
        *,
        cancel_event: threading.Event | None = None,
    ) -> tuple[str, int, int]:
        entered.set()
        time.sleep(0.15)
        return orig_compress(self, src, crash_id_arg, cancel_event=cancel_event)

    monkeypatch.setattr(StorageManager, "compress_dump", _slow_compress)

    async def _run_mat() -> None:
        async with api_harness.app.state.session_factory() as session:
            crash = await session.get(CrashReport, cid)
            assert crash is not None
            await materialize_staged_crash_artifacts(
                session, crash, mgr, api_harness.settings
            )

    task = asyncio.create_task(_run_mat())
    while not entered.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not list((api_harness.data_dir / "dumps").rglob("*"))


async def test_queued_memory_symbol_upload_protected_past_ttl_until_job_done(
    db_session: AsyncSession,
    tmp_data_dir: Path,
    test_settings: Settings,
) -> None:
    mgr = StorageManager(
        data_dir=tmp_data_dir,
        settings=test_settings,
        disk_usage_fn=make_disk_usage_fn(free_gb=40.0),
    )
    uid = uuid.uuid4().hex
    rel_dir = f"staging/symbol_uploads/{uid}"
    staged_pdb = mgr.atomic_write_bytes(f"{rel_dir}/LongQueue.pdb", b"PDB_BYTES")
    job_id = f"symbol-ingest:{uid}"
    lease_path = mgr.write_staging_queue_lease(
        rel_dir, ttl_seconds=1.0, job_id=job_id, queue_name=COMPUTE_QUEUE_NAME, mode="memory"
    )
    # Expire wall-clock TTL while job is still queued/active in memory dispatcher.
    data = json.loads(lease_path.read_text(encoding="utf-8"))
    data["expires_at"] = 1.0
    lease_path.write_text(json.dumps(data), encoding="utf-8")

    ACTIVE_MEMORY_SYMBOL_JOBS.add(job_id)
    try:
        await mgr.run_gc(db_session, force_emergency=True)
        assert staged_pdb.is_file()
    finally:
        ACTIVE_MEMORY_SYMBOL_JOBS.discard(job_id)

    # Once job is no longer queued/active, GC reclaims the orphaned staging file.
    await mgr.run_gc(db_session, force_emergency=True)
    assert not staged_pdb.exists()


async def test_materialize_retry_after_reconciliation_does_not_double_charge_dump(
    api_harness: Any,
) -> None:
    client = api_harness.client
    proj = (
        await client.post(
            "/api/v1/projects",
            json={"key": "retry-idemp", "name": "Retry Idemp"},
        )
    ).json()
    pid = uuid.UUID(proj["id"])
    cid = uuid.uuid4()
    mgr: StorageManager = api_harness.app.state.storage
    dump_rel = f"staging/uploads/{cid}/dump.raw"
    raw_path = mgr.atomic_write_bytes(dump_rel, b"MDMP_RETRY_IDEMPOTENT_BYTES")

    # Simulate a crashed prior attempt that compressed the dump before committing,
    # followed by GC reconciliation counting that compressed dump in the ledger.
    _, _, expected_comp_size = mgr.compress_dump(raw_path, cid)
    async with api_harness.app.state.session_factory() as session:
        session.add(
            CrashReport(
                id=cid,
                project_id=pid,
                status="PARSING",
                app_version="1.0.0",
                dump_path=dump_rel,
            )
        )
        await session.commit()
        async with mgr.storage_io_lock(exclusive=True):
            await mgr.reconcile_usage(session)
            await session.commit()

        crash = await session.get(CrashReport, cid)
        assert crash is not None
        await materialize_staged_crash_artifacts(session, crash, mgr, api_harness.settings)

        dumps_row = await session.get(StoragePartitionUsage, "dumps")
        staging_row = await session.get(StoragePartitionUsage, "staging")
        assert dumps_row is not None
        assert dumps_row.used_bytes == expected_comp_size
        assert dumps_row.file_count == 1
        assert staging_row is not None and staging_row.used_bytes == 0


async def test_symbol_ingest_preserves_staged_files_on_cancelled_retry(
    api_harness: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import symbol_service

    client = api_harness.client
    proj = (
        await client.post(
            "/api/v1/projects",
            json={"key": "sym-cancel-retry", "name": "Sym Cancel Retry"},
        )
    ).json()
    pid = proj["id"]
    mgr: StorageManager = api_harness.app.state.storage
    uid = uuid.uuid4().hex
    rel_dir = f"staging/symbol_uploads/{uid}"
    pdb_abs = mgr.atomic_write_bytes(f"{rel_dir}/Retry.pdb", b"VALID_PDB")
    lease_abs = mgr.write_staging_queue_lease(
        rel_dir, ttl_seconds=600.0, job_id=f"symbol-ingest:{uid}", mode="redis"
    )

    orig_ingest = symbol_service.ingest_symbol
    first_attempt = True

    async def _cancel_first(*args: Any, **kwargs: Any) -> Any:
        nonlocal first_attempt
        if first_attempt:
            first_attempt = False
            raise asyncio.CancelledError()
        return await orig_ingest(*args, **kwargs)

    monkeypatch.setattr(symbol_service, "ingest_symbol", _cancel_first)
    ctx = {
        "settings": api_harness.settings,
        "session_factory": api_harness.app.state.session_factory,
        "storage": mgr,
        "event_bus": InMemoryEventBus(),
        "exec_fn": api_harness.app.state.dispatcher.exec_fn,
    }

    with pytest.raises(asyncio.CancelledError):
        await worker_tasks.symbol_ingest(ctx, pid, "1.0.0", str(pdb_abs), "Retry.pdb")

    # Staged input and queue lease MUST remain intact for ARQ retry.
    assert pdb_abs.is_file()
    assert lease_abs.is_file()

    # Retry succeeds and cleans up both staged file and lease.
    out = await worker_tasks.symbol_ingest(ctx, pid, "1.0.0", str(pdb_abs), "Retry.pdb")
    assert out["symbol_id"]
    assert not pdb_abs.exists()
    assert not lease_abs.exists()


async def test_redis_event_bus_propagates_failed_subscribe_to_waiter_and_promotes_on_cancel(
) -> None:
    from app.services.events import crash_channel

    entered_b = asyncio.Event()
    release_b = asyncio.Event()
    fail_channels: set[str] = set()
    subscribed: set[str] = set()
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    class SelectivePubSub:
        async def subscribe(self, channel: str) -> None:
            if channel in fail_channels:
                entered_b.set()
                await release_b.wait()
                raise ConnectionError("subscribe rejected")
            subscribed.add(channel)

        async def unsubscribe(self, channel: str) -> None:
            subscribed.discard(channel)

        async def get_message(self, **kwargs: Any) -> dict[str, Any] | None:
            timeout = float(kwargs.get("timeout") or 0.02)
            try:
                return await asyncio.wait_for(incoming.get(), timeout=timeout)
            except TimeoutError:
                return None

        async def aclose(self) -> None:
            return None

    class SelectiveRedis:
        def pubsub(self) -> SelectivePubSub:
            return SelectivePubSub()

        async def publish(self, channel: str, payload: str) -> int:
            if channel in subscribed:
                await incoming.put({"type": "message", "channel": channel, "data": payload})
                return 1
            return 0

    bus = RedisEventBus(
        "redis://localhost:6379/0", redis_client=SelectiveRedis(), poll_interval=0.02
    )
    cid_a = uuid.uuid4()
    cid_b = uuid.uuid4()
    fail_channels.add(crash_channel(cid_b))

    try:
        async with bus.subscribe(cid_a):
            # Part 1: leader fails SUBSCRIBE on cid_b while cid_a keeps shared _pubsub alive.
            t1 = asyncio.create_task(bus.subscribe(cid_b).__aenter__())
            await asyncio.wait_for(entered_b.wait(), timeout=1.0)
            t2 = asyncio.create_task(bus.subscribe(cid_b).__aenter__())
            await asyncio.sleep(0.02)
            release_b.set()

            with pytest.raises(ConnectionError, match="subscribe rejected"):
                await t1
            with pytest.raises(ConnectionError, match="subscribe rejected"):
                await t2
            assert crash_channel(cid_b) not in subscribed

            # Part 2: leader is cancelled before SUBSCRIBE finishes -> waiter takes over & succeeds.
            entered_b.clear()
            release_b.clear()
            cid_c = uuid.uuid4()
            ch_c = crash_channel(cid_c)
            fail_channels.add(ch_c)

            t3 = asyncio.create_task(bus.subscribe(cid_c).__aenter__())
            await asyncio.wait_for(entered_b.wait(), timeout=1.0)
            t4 = asyncio.create_task(bus.subscribe(cid_c).__aenter__())
            await asyncio.sleep(0.02)
            fail_channels.discard(ch_c)
            t3.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t3
            sub4 = await asyncio.wait_for(t4, timeout=1.0)
            assert ch_c in subscribed
            await sub4.close()
    finally:
        await bus.close()


async def test_scratch_reservation_blocks_concurrent_staging_oversubscription(
    db_session: AsyncSession,
    tmp_data_dir: Path,
    test_settings: Settings,
) -> None:
    from app.services.storage import StorageCapacityError

    tight_settings = test_settings.model_copy(
        update={"STAGING_QUOTA_GB": 100 / (1024**3)}
    )
    mgr = StorageManager(
        data_dir=tmp_data_dir,
        settings=tight_settings,
        disk_usage_fn=make_disk_usage_fn(free_gb=200.0),
    )

    with mgr.staging_workspace("sym") as ws:
        await mgr.reserve_staging_scratch(db_session, ws, 60)
        await db_session.commit()

        # Even before any bytes are copied into ws, a concurrent 60B upload must be rejected.
        with pytest.raises(StorageCapacityError):
            await mgr.commit_usage_deltas(
                db_session,
                {"staging": 60},
                {"staging": 1},
                enforce_quota=True,
            )

    # Once workspace exits, 60B upload fits cleanly.
    await mgr.commit_usage_deltas(
        db_session,
        {"staging": 60},
        {"staging": 1},
        enforce_quota=True,
    )
    await db_session.commit()


async def test_log_is_indexing_flag_and_stackwalk_public_download_ledger(
    api_harness: Any,
) -> None:
    client = api_harness.client
    upd = await client.put(
        "/api/v1/settings",
        json={
            "enable_public_symbols": True,
            "public_symbol_urls": ["https://symbols.mozilla.org/"],
        },
    )
    assert upd.status_code == 200
    proj = (
        await client.post(
            "/api/v1/projects",
            json={
                "key": "idx-pub-test",
                "name": "Idx Pub Test",
                "settings_json": {"ai_triage_mode": "MANUAL"},
            },
        )
    ).json()

    orig_exec = api_harness.app.state.dispatcher.exec_fn

    async def _downloading_stackwalk(*cmd: str, **kwargs: Any) -> Any:
        # Simulate minidump-stackwalk downloading a public .sym into --symbols-cache.
        if "--symbols-cache" in cmd:
            cache_dir = Path(cmd[cmd.index("--symbols-cache") + 1])
            target = cache_dir / "AutoDl.pdb" / "11223344" / "AutoDl.sym"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"MODULE windows x86_64 11223344 AutoDl.pdb\n" + b"X" * 200)
        return await orig_exec(*cmd, **kwargs)

    api_harness.app.state.dispatcher.exec_fn = _downloading_stackwalk

    acquired = 0
    for _ in range(api_harness.settings.COMPUTE_WORKER_MAX_JOBS):
        await api_harness.app.state.dispatcher._memory_compute_slots.acquire()
        acquired += 1

    try:
        up = await client.post(
            "/api/v1/crashes/upload",
            headers={"X-API-Key": proj["api_key"]},
            data={"project_key": proj["key"], "app_version": "1.0.0"},
            files=[
                ("dump_file", ("sample.dmp", b"MDMP_DL_TEST")),
                ("log_files", ("app.log", b"2026-11-28 10:00:00 INFO queued\n")),
            ],
        )
        assert up.status_code == 202
        crash_id = up.json()["crash_id"]
        log_id = up.json()["log_ids"][0]

        # While queued before Stage 0 runs, CrashDetail and LogSummary expose is_indexing=True.
        detail_pending = (await client.get(f"/api/v1/crashes/{crash_id}")).json()
        assert detail_pending["logs"][0]["is_indexing"] is True
        summary_pending = (await client.get(f"/api/v1/logs/{log_id}/summary")).json()
        assert summary_pending["is_indexing"] is True
    finally:
        for _ in range(acquired):
            api_harness.app.state.dispatcher._memory_compute_slots.release()

    await _drain(api_harness)

    # After worker finishes: is_indexing=False AND stackwalk's downloaded public .sym is in ledger.
    detail_done = (await client.get(f"/api/v1/crashes/{crash_id}")).json()
    assert detail_done["logs"][0]["is_indexing"] is False
    assert detail_done["logs"][0]["total_lines"] == 1

    async with api_harness.app.state.session_factory() as session:
        pub_row = await session.get(StoragePartitionUsage, "symbols/public_cache")
        assert pub_row is not None
        assert pub_row.used_bytes >= 200
        assert pub_row.file_count >= 1
