"""Unit tests for settings_service, events, and workers (tasks + TaskDispatcher)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.models import CrashReport, Project, SystemSetting
from app.services import settings_service
from app.services.events import (
    InMemoryEventBus,
    RedisEventBus,
    close_event_bus,
    create_event_bus,
    get_event_bus,
    set_event_bus,
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
from tests.conftest import make_disk_usage_fn, make_mock_exec


def _sample_stackwalk_json(
    function_name: str | None = "App::CrashFunc()",
    missing: bool = False,
) -> bytes:
    frames = [
        {
            "frame": 0,
            "module": "App.exe",
            "module_offset": "0x1234",
            "function": function_name,
            "function_offset": "0x10" if function_name else None,
            "file": "main.cpp" if function_name else None,
            "line": 42 if function_name else None,
            "trust": "cfi",
            "missing_symbols": missing,
        }
    ]
    modules = [
        {
            "filename": "App.exe",
            "debug_file": "App.pdb",
            "debug_id": "AAAABBBBCCCCDDDD11112222333344441",
            "code_file": "App.exe",
            "code_id": "12345678",
            "loaded_symbols": not missing,
            "missing_symbols": missing,
        }
    ]
    payload = {
        "status": "OK",
        "crash_info": {
            "type": "EXCEPTION_ACCESS_VIOLATION_READ",
            "address": "0x0",
            "crashing_thread": 0,
        },
        "system_info": {"os": "Windows NT", "os_ver": "10.0.22621", "cpu_arch": "amd64"},
        "crashing_thread": {"thread_index": 0, "frames": frames},
        "threads": [{"thread_index": 0, "thread_id": 1, "thread_name": "main", "frames": frames}],
        "modules": modules,
    }
    return json.dumps(payload).encode("utf-8")


# ---------------------------------------------------------------------------
# settings_service tests
# ---------------------------------------------------------------------------


async def test_settings_defaults_partial_update_and_api_key_lifecycle(
    db_session: AsyncSession,
    tmp_data_dir: Path,
) -> None:
    base = Settings(
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        DATA_DIR=str(tmp_data_dir),
        LLM_API_KEY="sk-env-secret-9876",
        ARCHIVE_RAW_SYMBOLS=False,
    )
    eff = await settings_service.get_effective_settings(db_session, base_settings=base)
    assert eff.llm_api_key == "sk-env-secret-9876"
    assert eff.llm_api_key_set is True
    assert eff.llm_api_key_masked is not None
    assert "9876" in eff.llm_api_key_masked
    assert "sk-env-secret-9876" not in eff.llm_api_key_masked
    assert "llm_api_key" not in eff.to_public_dict()
    assert eff.default_noise_modules == list(settings_service.DEFAULT_NOISE_MODULES)
    assert eff.default_noise_functions == list(settings_service.DEFAULT_NOISE_FUNCTIONS)

    # Partial update preserves llm_api_key when omitted, None, or masked.
    eff2 = await settings_service.update_settings(
        db_session,
        {
            "llm_model": "gpt-4o",
            "archive_raw_symbols": True,
            "host_safety_floor_gb": 25.0,
            "llm_api_key": eff.llm_api_key_masked,
        },
        base_settings=base,
    )
    assert eff2.llm_model == "gpt-4o"
    assert eff2.llm_api_key == "sk-env-secret-9876"
    assert eff2.archive_raw_symbols is True
    app_cfg = eff2.to_app_settings(base_settings=base)
    assert app_cfg.ARCHIVE_RAW_SYMBOLS is True
    assert app_cfg.HOST_SAFETY_FLOOR_GB == 25.0
    assert app_cfg.LLM_MODEL == "gpt-4o"

    # Setting llm_api_key="" persists {"value": ""} and overrides non-empty env LLM_API_KEY.
    eff_cleared = await settings_service.update_settings(
        db_session,
        {"llm_api_key": ""},
        base_settings=base,
    )
    assert eff_cleared.llm_api_key == ""
    assert eff_cleared.llm_api_key_set is False
    assert eff_cleared.llm_api_key_masked is None


async def test_settings_validation_corrupt_row_and_project_resolution(
    db_session: AsyncSession,
    test_settings: Settings,
) -> None:
    with pytest.raises(ValidationError):
        settings_service.SystemSettingsUpdate(total_disk_quota_gb=-1.0)
    with pytest.raises(ValidationError):
        settings_service.SystemSettingsUpdate(gc_threshold_percent=0.0)
    with pytest.raises(ValidationError):
        settings_service.SystemSettingsUpdate(ai_triage_mode="INVALID_MODE")  # type: ignore[arg-type]

    # Legacy scalar + corrupt DB row should not crash get_effective_settings.
    db_session.add(SystemSetting(key="ai_max_steps", value_json=5))
    db_session.add(SystemSetting(key="gc_threshold_percent", value_json={"value": -99}))
    await db_session.commit()

    eff = await settings_service.get_effective_settings(db_session, base_settings=test_settings)
    assert eff.ai_max_steps == 5
    assert eff.gc_threshold_percent == test_settings.GC_THRESHOLD_PERCENT

    proj_cfg = settings_service.resolve_project_settings(
        eff,
        {
            "ai_triage_mode": "manual",
            "noise_modules": ["CustomWrap.dll"],
            "custom_crash_handlers": ["crashpad.dll", "ReportCrash"],
        },
    )
    assert proj_cfg["ai_triage_mode"] == "MANUAL"
    assert proj_cfg["noise_modules"]
    assert "ReportCrash" in proj_cfg["noise_functions"]

    # Non-finite floats must be rejected by both SystemSettingsUpdate and EffectiveSettings.
    for bad_float in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValidationError):
            settings_service.SystemSettingsUpdate(total_disk_quota_gb=bad_float)
        with pytest.raises(ValidationError):
            settings_service.EffectiveSettings(total_disk_quota_gb=bad_float)

    # Booleans must not be coerced to int retention days in resolve_project_settings.
    bool_proj_cfg = settings_service.resolve_project_settings(
        eff,
        {"log_retention_days": True, "dump_retention_days": False},
    )
    assert bool_proj_cfg["log_retention_days"] == eff.log_retention_days
    assert bool_proj_cfg["dump_retention_days"] == eff.dump_retention_days


async def test_settings_rejects_booleans_on_all_numeric_fields(
    db_session: AsyncSession,
    test_settings: Settings,
) -> None:
    numeric_fields = (
        "ai_max_steps",
        "ai_timeout_seconds",
        "ai_tool_output_max_chars",
        "total_disk_quota_gb",
        "host_safety_floor_gb",
        "db_quota_gb",
        "symbols_sym_quota_gb",
        "symbols_public_cache_quota_gb",
        "symbols_raw_quota_gb",
        "logs_quota_gb",
        "dumps_quota_gb",
        "staging_quota_gb",
        "gc_threshold_percent",
        "log_retention_days",
        "dump_retention_days",
        "staging_ttl_hours",
    )
    for field in numeric_fields:
        for bool_val in (True, False):
            with pytest.raises(ValidationError):
                settings_service.SystemSettingsUpdate.model_validate({field: bool_val})
            with pytest.raises(ValidationError):
                settings_service.EffectiveSettings.model_validate({field: bool_val})

    # update_settings must reject booleans on numeric fields before persisting anything.
    baseline = await settings_service.get_effective_settings(
        db_session, base_settings=test_settings
    )
    for key, bad_val in (
        ("log_retention_days", True),
        ("dump_retention_days", False),
        ("total_disk_quota_gb", False),
        ("gc_threshold_percent", True),
        ("ai_max_steps", True),
        ("staging_ttl_hours", True),
    ):
        with pytest.raises(ValidationError):
            await settings_service.update_settings(
                db_session,
                {key: bad_val},
                base_settings=test_settings,
            )

    after_rejected = await settings_service.get_effective_settings(
        db_session, base_settings=test_settings
    )
    assert after_rejected.log_retention_days == baseline.log_retention_days
    assert after_rejected.total_disk_quota_gb == baseline.total_disk_quota_gb
    assert after_rejected.gc_threshold_percent == baseline.gc_threshold_percent

    # Legitimate boolean fields continue accepting bool values.
    updated_bools = await settings_service.update_settings(
        db_session,
        {"archive_raw_symbols": True, "enable_public_symbols": False},
        base_settings=test_settings,
    )
    assert updated_bools.archive_raw_symbols is True
    assert updated_bools.enable_public_symbols is False

    # Corrupt boolean DB rows on numeric keys must be skipped and fall back to env defaults.
    db_session.add(SystemSetting(key="log_retention_days", value_json={"value": True}))
    db_session.add(SystemSetting(key="total_disk_quota_gb", value_json=False))
    db_session.add(SystemSetting(key="gc_threshold_percent", value_json={"value": True}))
    await db_session.commit()

    recovered = await settings_service.get_effective_settings(
        db_session, base_settings=test_settings
    )
    assert recovered.log_retention_days == test_settings.LOG_RETENTION_DAYS
    assert recovered.total_disk_quota_gb == test_settings.TOTAL_DISK_QUOTA_GB
    assert recovered.gc_threshold_percent == test_settings.GC_THRESHOLD_PERCENT


async def test_settings_llm_provider_and_thinking_level_lifecycle(
    db_session: AsyncSession,
    test_settings: Settings,
) -> None:
    baseline = await settings_service.get_effective_settings(
        db_session, base_settings=test_settings
    )
    assert baseline.llm_provider == "openai"
    assert baseline.llm_thinking_level == "auto"
    public = baseline.to_public_dict()
    assert public["llm_provider"] == "openai"
    assert public["llm_thinking_level"] == "auto"

    # Valid update with mixed case normalizes to lowercase literals.
    updated = await settings_service.update_settings(
        db_session,
        {
            "llm_provider": "Anthropic",
            "llm_base_url": "https://api.anthropic.com",
            "llm_model": "claude-sonnet-4-5",
            "llm_thinking_level": "HIGH",
        },
        base_settings=test_settings,
    )
    assert updated.llm_provider == "anthropic"
    assert updated.llm_model == "claude-sonnet-4-5"
    assert updated.llm_thinking_level == "high"
    app_cfg = updated.to_app_settings(base_settings=test_settings)
    assert app_cfg.LLM_PROVIDER == "anthropic"
    assert app_cfg.LLM_THINKING_LEVEL == "high"

    # Reject unknown provider or thinking level.
    with pytest.raises(ValidationError):
        settings_service.SystemSettingsUpdate(llm_provider="unknown_vendor")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        settings_service.SystemSettingsUpdate(llm_thinking_level="extreme")  # type: ignore[arg-type]

    # Corrupt DB values fall back cleanly to defaults.
    await db_session.merge(SystemSetting(key="llm_provider", value_json={"value": "bad_vendor"}))
    await db_session.merge(
        SystemSetting(key="llm_thinking_level", value_json={"value": "bad_level"})
    )
    await db_session.commit()

    recovered = await settings_service.get_effective_settings(
        db_session, base_settings=test_settings
    )
    assert recovered.llm_provider == test_settings.LLM_PROVIDER
    assert recovered.llm_thinking_level == test_settings.LLM_THINKING_LEVEL


# ---------------------------------------------------------------------------
# events.py tests
# ---------------------------------------------------------------------------


async def test_event_bus_factory_and_rebind(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        assert isinstance(create_event_bus("memory://"), InMemoryEventBus)
        assert isinstance(create_event_bus("redis://localhost:6379/0"), RedisEventBus)
        with pytest.raises(ValueError):
            create_event_bus("amqp://localhost/")

        monkeypatch.setenv("REDIS_URL", "memory://")
        get_settings.cache_clear()
        set_event_bus(None)
        assert isinstance(get_event_bus(), InMemoryEventBus)

        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/1")
        get_settings.cache_clear()
        assert isinstance(get_event_bus(), RedisEventBus)
    finally:
        await close_event_bus()
        get_settings.cache_clear()


async def test_in_memory_event_bus_fanout_isolation_eviction_and_close() -> None:
    bus = InMemoryEventBus(max_queue_size=2)
    cid1 = uuid.uuid4()
    cid2 = uuid.uuid4()

    async with (
        bus.subscribe(cid1) as sub1a,
        bus.subscribe(cid1) as sub1b,
        bus.subscribe(cid2) as sub2,
    ):
        for st in ("PENDING", "PARSING"):
            await bus.publish_status(cid1, st, stage=st.lower())
        await bus.publish_status(cid2, "ANALYZED")

        e1a = [await sub1a.get(timeout=1.0), await sub1a.get(timeout=1.0)]
        e1b = [await sub1b.get(timeout=1.0), await sub1b.get(timeout=1.0)]
        e2 = await sub2.get(timeout=1.0)
        assert [e["status"] for e in e1a] == ["PENDING", "PARSING"]
        assert [e["status"] for e in e1b] == ["PENDING", "PARSING"]
        assert e2["crash_id"] == str(cid2)

        # Full queue drops oldest without blocking publish.
        await bus.publish_status(cid1, "PARSING", stage="s1")
        await bus.publish_status(cid1, "TRIAGING", stage="s2")
        await bus.publish_status(cid1, "ANALYZED", stage="s3")
        ev_kept = [await sub1a.get(timeout=1.0), await sub1a.get(timeout=1.0)]
        assert [e["status"] for e in ev_kept] == ["TRIAGING", "ANALYZED"]

    # After context exit, subscriber queues are cleaned up.
    assert not bus._subscribers

    # close() terminates active async iterator cleanly.
    sub_close = bus.subscribe(cid1)
    await bus.publish_status(cid1, "PARSING")
    await bus.close()
    collected = [ev["status"] async for ev in sub_close]
    assert collected == ["PARSING"]


async def test_redis_event_bus_publish_and_subscribe_with_mock_client() -> None:
    published: list[tuple[str, str]] = []
    unsubscribed: list[str] = []
    closed_pubsub = False

    class FakePubSub:
        def __init__(self) -> None:
            self.msgs: list[dict[str, Any]] = []

        async def subscribe(self, channel: str) -> None:
            self.msgs.append(
                {
                    "type": "message",
                    "data": json.dumps(
                        {
                            "crash_id": channel.split(":")[2],
                            "status": "ANALYZED",
                            "stage": "done",
                            "ai_verdict": "NEW_ISSUE",
                            "error": None,
                        }
                    ),
                }
            )

        async def get_message(self, **_kwargs: Any) -> dict[str, Any] | None:
            return self.msgs.pop(0) if self.msgs else None

        async def unsubscribe(self, channel: str) -> None:
            unsubscribed.append(channel)

        async def aclose(self) -> None:
            nonlocal closed_pubsub
            closed_pubsub = True

    fake_pubsub = FakePubSub()

    class FakeRedis:
        async def publish(self, channel: str, msg: str) -> int:
            published.append((channel, msg))
            return 1

        def pubsub(self) -> FakePubSub:
            return fake_pubsub

    bus = RedisEventBus("redis://localhost:6379/0", redis_client=FakeRedis(), poll_interval=0.05)
    cid = uuid.uuid4()
    await bus.publish_status(cid, "PARSING", stage="parsing_dump")
    assert len(published) == 1
    assert json.loads(published[0][1])["status"] == "PARSING"

    async with bus.subscribe(cid) as sub:
        ev = await sub.get(timeout=1.0)
        assert ev["status"] == "ANALYZED"
        assert ev["ai_verdict"] == "NEW_ISSUE"

    assert len(unsubscribed) == 1
    assert closed_pubsub is True


async def test_redis_subscription_cleans_up_on_subscribe_cancel_and_error() -> None:
    async def _run_case(*, cancel: bool) -> None:
        aclose_calls = 0
        subscribe_started = asyncio.Event()

        class FailingPubSub:
            async def subscribe(self, _channel: str) -> None:
                subscribe_started.set()
                if cancel:
                    await asyncio.sleep(10)
                raise ConnectionError("redis down")

            async def unsubscribe(self, _channel: str) -> None:
                pass

            async def aclose(self) -> None:
                nonlocal aclose_calls
                aclose_calls += 1

        class FakeRedis:
            def pubsub(self) -> FailingPubSub:
                return FailingPubSub()

        bus = RedisEventBus(
            "redis://localhost:6379/0",
            redis_client=FakeRedis(),
            poll_interval=0.05,
        )
        cid = uuid.uuid4()

        async def _enter() -> None:
            async with bus.subscribe(cid):
                pass

        if cancel:
            task = asyncio.create_task(_enter())
            await subscribe_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ConnectionError):
                await _enter()

        assert aclose_calls == 1
        assert len(bus._active_subscriptions) == 0
        await bus.close()

    await _run_case(cancel=True)
    await _run_case(cancel=False)


# ---------------------------------------------------------------------------
# workers & tasks tests
# ---------------------------------------------------------------------------


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


async def test_resymbolicate_partial_failure_isolates_crashes_no_missing_greenlet(
    async_engine: AsyncEngine,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """A corrupt dump early in a resymbolicate batch must mark only that crash FAILED
    and continue resymbolicating all subsequent crashes without MissingGreenlet.
    """
    session_factory = async_sessionmaker(
        bind=async_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    bus = InMemoryEventBus()

    async with session_factory() as session:
        proj = Project(
            key="partial-resym-proj",
            name="Partial Resym Project",
            api_key_hash=hashlib.sha256(b"k").hexdigest(),
            settings_json={"ai_triage_mode": "MANUAL"},
        )
        session.add(proj)
        await session.flush()

        c1_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        c2_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        c3_id = uuid.UUID("22222222-2222-2222-2222-222222222222")

        bad_rel = f"dumps/{c1_id}/{c1_id}.dmp.zst"
        storage_manager.atomic_write_bytes(bad_rel, b"CORRUPT_ZSTD_PAYLOAD")

        dmp2 = tmp_path / "valid2.dmp"
        dmp2.write_bytes(b"MDMP_VALID_2")
        rel2, rsz2, csz2 = storage_manager.compress_dump(dmp2, c2_id)

        dmp3 = tmp_path / "valid3.dmp"
        dmp3.write_bytes(b"MDMP_VALID_3")
        rel3, rsz3, csz3 = storage_manager.compress_dump(dmp3, c3_id)

        missing_entry = [{"debug_file": "App.pdb", "debug_id": "DEAD1111BEEF2222"}]
        session.add_all(
            [
                CrashReport(
                    id=c1_id,
                    project_id=proj.id,
                    status="ANALYZED",
                    app_version="1.0.0",
                    dump_path=bad_rel,
                    missing_symbols_json=missing_entry,
                ),
                CrashReport(
                    id=c2_id,
                    project_id=proj.id,
                    status="ANALYZED",
                    app_version="1.0.0",
                    dump_path=rel2,
                    dump_raw_size=rsz2,
                    dump_compressed_size=csz2,
                    missing_symbols_json=missing_entry,
                ),
                CrashReport(
                    id=c3_id,
                    project_id=proj.id,
                    status="ANALYZED",
                    app_version="1.0.0",
                    dump_path=rel3,
                    dump_raw_size=rsz3,
                    dump_compressed_size=csz3,
                    missing_symbols_json=missing_entry,
                ),
            ]
        )
        await session.commit()

    ctx = {
        "settings": test_settings,
        "storage": storage_manager,
        "session_factory": session_factory,
        "event_bus": bus,
        "exec_fn": make_mock_exec(stdout=_sample_stackwalk_json("App::Fixed()", missing=False)),
    }

    async with (
        bus.subscribe(c1_id) as sub1,
        bus.subscribe(c2_id) as sub2,
        bus.subscribe(c3_id) as sub3,
    ):
        res = await resymbolicate(
            ctx,
            crash_ids=[str(c1_id), str(c2_id), str(c3_id)],
            debug_id="DEAD1111BEEF2222",
        )
        ev1 = await sub1.get(timeout=1.0)
        ev2 = await sub2.get(timeout=1.0)
        ev3 = await sub3.get(timeout=1.0)

    assert res["resymbolicated_count"] == 2
    assert set(res["crash_ids"]) == {str(c2_id), str(c3_id)}
    assert ev1["status"] == "FAILED"
    assert ev1["error"]
    assert ev2["status"] == "ANALYZED"
    assert ev3["status"] == "ANALYZED"

    async with session_factory() as verify_session:
        r1 = await verify_session.get(CrashReport, c1_id)
        r2 = await verify_session.get(CrashReport, c2_id)
        r3 = await verify_session.get(CrashReport, c3_id)
        assert r1 is not None and r1.status == "FAILED"
        assert r2 is not None and r2.status == "ANALYZED" and r2.missing_symbols_json == []
        assert r3 is not None and r3.status == "ANALYZED" and r3.missing_symbols_json == []
    await bus.close()


async def test_redis_subscription_closes_pubsub_on_concurrent_bus_close() -> None:
    """bus.close() running while pubsub.subscribe() is in-flight must not leak pubsub."""
    aclose_calls = 0
    subscribe_entered = asyncio.Event()
    allow_subscribe_return = asyncio.Event()

    class RacingPubSub:
        async def subscribe(self, _channel: str) -> None:
            subscribe_entered.set()
            await allow_subscribe_return.wait()

        async def get_message(self, **_kwargs: Any) -> dict[str, Any] | None:
            return None

        async def unsubscribe(self, _channel: str) -> None:
            pass

        async def aclose(self) -> None:
            nonlocal aclose_calls
            aclose_calls += 1

    class FakeRedis:
        def pubsub(self) -> RacingPubSub:
            return RacingPubSub()

    bus = RedisEventBus("redis://localhost:6379/0", redis_client=FakeRedis(), poll_interval=0.05)
    cid = uuid.uuid4()

    collected: list[dict[str, Any]] = []

    async def _consume() -> None:
        async for ev in bus.iter_events(cid):
            collected.append(ev)

    consumer = asyncio.create_task(_consume())
    await subscribe_entered.wait()
    await bus.close()
    allow_subscribe_return.set()
    await consumer

    assert collected == []
    assert aclose_calls == 1
    assert len(bus._active_subscriptions) == 0


async def test_tasks_expire_on_commit_true_and_resymbolicate_empty_list(
    async_engine: AsyncEngine,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """All tasks must work with SQLAlchemy default expire_on_commit=True without MissingGreenlet,
    and resymbolicate(crash_ids=[]) must return 0 without touching existing crashes.
    """
    session_factory = async_sessionmaker(
        bind=async_engine, class_=AsyncSession, expire_on_commit=True, autoflush=False
    )
    bus = InMemoryEventBus()

    async with session_factory() as session:
        proj = Project(
            key="exp-true-proj",
            name="Expire On Commit True Project",
            api_key_hash=hashlib.sha256(b"k").hexdigest(),
            settings_json={"ai_triage_mode": "SMART_TRIAGE"},
        )
        session.add(proj)
        await session.flush()
        proj_id = proj.id
        await session.commit()

    # 1. symbol_ingest under expire_on_commit=True
    staged_pdb = storage_manager.partition_path("staging") / "exp.pdb"
    staged_pdb.write_bytes(b"FAKE_PDB")
    sym_bytes = b"MODULE windows x86_64 999988887777666655554444333322221 Exp.pdb\n"
    sym_res = await symbol_ingest(
        {
            "settings": test_settings,
            "storage": storage_manager,
            "session_factory": session_factory,
            "event_bus": bus,
            "exec_fn": make_mock_exec(stdout=sym_bytes),
        },
        project_id=str(proj_id),
        app_version="1.0.0",
        pdb_staged_path=str(staged_pdb),
        pdb_filename="Exp.pdb",
    )
    assert sym_res["debug_file"] == "Exp.pdb"
    assert not staged_pdb.exists()

    class MockLLM:
        def __init__(self) -> None:
            async def _create(**kw: Any) -> Any:
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
                                            id="ov_exp",
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
                        usage=SimpleNamespace(prompt_tokens=15, completion_tokens=8),
                    )
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps(
                                    {
                                        "verdict": "NEW_ISSUE",
                                        "confidence": 0.9,
                                        "suggested_title": "Exp Crash",
                                        "root_cause_analysis": "Null deref",
                                    }
                                ),
                                tool_calls=None,
                            )
                        )
                    ],
                    usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10),
                )

            self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))

    # 2. parse_crash (both should_run_ai=True and should_run_ai=False) under expire_on_commit=True
    c1_id = uuid.uuid4()
    c2_id = uuid.uuid4()
    async with session_factory() as session:
        for cid_val, fname in ((c1_id, "e1.dmp"), (c2_id, "e2.dmp")):
            raw = tmp_path / fname
            raw.write_bytes(b"MDMP_EXP")
            rel_d, rsz, csz = storage_manager.compress_dump(raw, cid_val)
            session.add(
                CrashReport(
                    id=cid_val,
                    project_id=proj_id,
                    status="PENDING",
                    app_version="1.0.0",
                    dump_path=rel_d,
                    dump_raw_size=rsz,
                    dump_compressed_size=csz,
                )
            )
        await session.commit()

    base_ctx = {
        "settings": test_settings,
        "storage": storage_manager,
        "session_factory": session_factory,
        "event_bus": bus,
        "exec_fn": make_mock_exec(stdout=_sample_stackwalk_json("Exp::CrashHere()")),
        "openai_client": MockLLM(),
    }

    async with bus.subscribe(c1_id) as sub1:
        res_p1 = await parse_crash(base_ctx, str(c1_id))
        evs1 = [
            await sub1.get(timeout=1.0),
            await sub1.get(timeout=1.0),
            await sub1.get(timeout=1.0),
        ]
    assert res_p1["status"] == "TRIAGING"
    assert res_p1["queued_ai"] is True
    assert [e["status"] for e in evs1] == ["PARSING", "TRIAGING", "ANALYZED"]

    res_p2 = await parse_crash(base_ctx, str(c2_id))
    assert res_p2["status"] == "ANALYZED"
    assert res_p2["queued_ai"] is False

    # 3. ai_triage directly from status="ANALYZED" (triggers commit+refresh path)
    async with bus.subscribe(c2_id) as sub2:
        triage_res = await ai_triage(base_ctx, str(c2_id))
        tev1 = await sub2.get(timeout=1.0)
        tev2 = await sub2.get(timeout=1.0)
    assert triage_res["status"] == "ANALYZED"
    assert triage_res["ai_verdict"] == "NEW_ISSUE"
    assert [tev1["status"], tev2["status"]] == ["TRIAGING", "ANALYZED"]

    # 4. resymbolicate(crash_ids=[]) must return 0 and do nothing.
    empty_res = await resymbolicate(base_ctx, crash_ids=[], project_id=str(proj_id))
    assert empty_res == {"resymbolicated_count": 0, "crash_ids": []}

    # 5. resymbolicate 4-crash batch (2 failing + 2 valid) under expire_on_commit=True
    b0 = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000000")
    b1 = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")
    b2 = uuid.UUID("cccccccc-0000-0000-0000-000000000002")
    b3 = uuid.UUID("dddddddd-0000-0000-0000-000000000003")

    async with session_factory() as session:
        bad0_rel = f"dumps/{b0}/{b0}.dmp.zst"
        storage_manager.atomic_write_bytes(bad0_rel, b"CORRUPT_ZSTD")

        dmp_b1 = tmp_path / "b1.dmp"
        dmp_b1.write_bytes(b"EXEC_FAIL_DMP")
        rel_b1, rsz_b1, csz_b1 = storage_manager.compress_dump(dmp_b1, b1)

        dmp_b2 = tmp_path / "b2.dmp"
        dmp_b2.write_bytes(b"VALID_KNOWN")
        rel_b2, rsz_b2, csz_b2 = storage_manager.compress_dump(dmp_b2, b2)

        dmp_b3 = tmp_path / "b3.dmp"
        dmp_b3.write_bytes(b"VALID_NEW")
        rel_b3, rsz_b3, csz_b3 = storage_manager.compress_dump(dmp_b3, b3)

        session.add_all(
            [
                CrashReport(
                    id=b0,
                    project_id=proj_id,
                    status="ANALYZED",
                    app_version="1.0.0",
                    dump_path=bad0_rel,
                ),
                CrashReport(
                    id=b1,
                    project_id=proj_id,
                    status="ANALYZED",
                    app_version="1.0.0",
                    dump_path=rel_b1,
                    dump_raw_size=rsz_b1,
                    dump_compressed_size=csz_b1,
                ),
                CrashReport(
                    id=b2,
                    project_id=proj_id,
                    status="ANALYZED",
                    app_version="1.0.0",
                    dump_path=rel_b2,
                    dump_raw_size=rsz_b2,
                    dump_compressed_size=csz_b2,
                ),
                CrashReport(
                    id=b3,
                    project_id=proj_id,
                    status="ANALYZED",
                    app_version="1.0.0",
                    dump_path=rel_b3,
                    dump_raw_size=rsz_b3,
                    dump_compressed_size=csz_b3,
                ),
            ]
        )
        await session.commit()

    async def batch_exec(*cmd: str, **_kwargs: Any) -> Any:
        dump_file = next(Path(arg) for arg in cmd if str(arg).endswith(".dmp"))
        dump_arg = dump_file.read_bytes()
        if dump_arg == b"EXEC_FAIL_DMP":
            return SimpleNamespace(
                returncode=1,
                communicate=lambda: _async_return((b"", b"corrupt minidump header")),
                kill=lambda: None,
            )
        if dump_arg == b"VALID_KNOWN":
            out = _sample_stackwalk_json("Exp::CrashHere()", missing=False)
        else:
            out = _sample_stackwalk_json("Exp::BrandNewFn()", missing=False)
        return SimpleNamespace(
            returncode=0,
            communicate=lambda o=out: _async_return((o, b"")),
            kill=lambda: None,
        )

    async def _async_return(val: Any) -> Any:
        return val

    batch_ctx = {
        **base_ctx,
        "exec_fn": batch_exec,
    }
    batch_res = await resymbolicate(
        batch_ctx,
        crash_ids=[str(b0), str(b1), str(b2), str(b3)],
    )
    assert batch_res["resymbolicated_count"] == 2
    assert set(batch_res["crash_ids"]) == {str(b2), str(b3)}

    async with session_factory() as verify_session:
        rb0 = await verify_session.get(CrashReport, b0)
        rb1 = await verify_session.get(CrashReport, b1)
        rb2 = await verify_session.get(CrashReport, b2)
        rb3 = await verify_session.get(CrashReport, b3)
        assert rb0 is not None and rb0.status == "FAILED"
        assert rb1 is not None and rb1.status == "FAILED"
        assert rb2 is not None and rb2.status == "ANALYZED"
        assert rb3 is not None and rb3.status == "ANALYZED"

    assert list(storage_manager.partition_path("staging").iterdir()) == []
    await bus.close()
