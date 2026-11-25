"""Integration tests for MSDL worker auto-resymbolication and force-sync workflows."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.base import Base
from app.db.models import AIAnalysisRun, CrashJob, CrashReport, Project
from app.db.session import create_async_engine_from_url
from app.services.events import InMemoryEventBus
from app.services.ms_symbol_service import (
    CIRCUIT_FAILURE_THRESHOLD,
    WindowsSymbolCandidate,
    fetch_and_import_windows_symbols,
    has_cached_symbol,
    has_cached_symbol_detail,
    is_circuit_open,
    parse_manual_ms_target,
)
from app.services.storage import StorageManager
from app.workers.jobs import stage_crash_job
from app.workers.tasks import fetch_ms_symbols
from app.workers.worker import TaskDispatcher
from tests._ms_symbol_helpers import (
    FAKE_PDB_BYTES,
    FAKE_PE_BYTES,
    KERNELBASE_DEBUG_ID,
    NTDLL_CODE_ID,
    NTDLL_DEBUG_ID,
    _sample_sym,
)
from tests.conftest import make_mock_exec


async def test_parse_crash_dispatches_ms_fetch_and_auto_resymbolicates_without_loop(
    tmp_path: Path,
    test_settings: Settings,
    storage_manager: StorageManager,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'msdl_e2e.db'}"
    engine = create_async_engine_from_url(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    settings = test_settings.model_copy(
        update={
            "DATABASE_URL": database_url,
            "REDIS_URL": "memory://",
            "AI_TRIAGE_MODE": "MANUAL",
            "MS_SYMBOL_FETCH_PE_UNWIND": False,
        }
    )
    bus = InMemoryEventBus()
    stackwalk_runs: list[tuple[str, ...]] = []
    msdl_http_calls: list[str] = []

    def http_handler(request: httpx.Request) -> httpx.Response:
        msdl_http_calls.append(str(request.url))
        if request.url.path.endswith(f"/ntdll.pdb/{NTDLL_DEBUG_ID}/ntdll.pdb"):
            return httpx.Response(200, content=FAKE_PDB_BYTES)
        return httpx.Response(404)

    async def unified_exec(*cmd: str, **_kwargs: Any) -> Any:
        bin_name = cmd[0]
        if bin_name == settings.DUMP_SYMS_BIN:
            stdout = _sample_sym("ntdll.pdb", NTDLL_DEBUG_ID)
        else:
            stackwalk_runs.append(cmd)
            resolved = has_cached_symbol(storage_manager, "ntdll.pdb", NTDLL_DEBUG_ID)
            payload = {
                "crash_info": {"type": "EXCEPTION_ACCESS_VIOLATION_READ", "address": "0x0"},
                "system_info": {"os": "Windows NT", "os_ver": "10.0.22631", "cpu": "amd64"},
                "crashing_thread": {
                    "threads_index": 0,
                    "frames": [
                        {
                            "frame": 0,
                            "offset": "0x7ff900001000",
                            "module": "ntdll.dll",
                            "function": "KiUserExceptionDispatcher" if resolved else None,
                            "missing_symbols": not resolved,
                        }
                    ],
                },
                "threads": [
                    {
                        "thread_id": 1,
                        "frames": [
                            {
                                "frame": 0,
                                "offset": "0x7ff900001000",
                                "module": "ntdll.dll",
                                "function": "KiUserExceptionDispatcher" if resolved else None,
                                "missing_symbols": not resolved,
                            }
                        ],
                    }
                ],
                "modules": [
                    {
                        "filename": r"C:\Windows\System32\ntdll.dll",
                        "debug_file": "ntdll.pdb",
                        "debug_id": NTDLL_DEBUG_ID,
                        "code_file": "ntdll.dll",
                        "code_id": NTDLL_CODE_ID,
                        "loaded_symbols": resolved,
                        "missing_symbols": not resolved,
                    }
                ],
            }
            stdout = json.dumps(payload).encode("utf-8")

        class _Proc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return stdout, b""

            def kill(self) -> None:
                pass

        return _Proc()

    dispatcher = TaskDispatcher(
        settings=settings,
        storage=storage_manager,
        event_bus=bus,
        session_factory=factory,
        exec_fn=unified_exec,
        http_transport=httpx.MockTransport(http_handler),
    )

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            project = Project(key="msdl-proj", name="MSDL Test", api_key_hash="hash")
            session.add(project)
            await session.flush()
            crash_id = uuid.uuid4()
            raw_dmp = tmp_path / "win.dmp"
            raw_dmp.write_bytes(b"MDMP_FAKE")
            dump_path, _, _ = storage_manager.compress_dump(raw_dmp, crash_id)
            session.add(
                CrashReport(
                    id=crash_id,
                    project_id=project.id,
                    status="PENDING",
                    app_version="1.0.0",
                    dump_path=dump_path,
                )
            )
            await session.flush()
            job_id = stage_crash_job(session, crash_id, "parse_crash")
            await session.commit()

        await dispatcher.kick_crash_jobs([job_id])
        # Wait for parse_crash -> fetch_ms_symbols -> auto-resymbolicate chain to settle.
        for _ in range(50):
            if dispatcher.pending_count == 0:
                async with factory() as session:
                    remaining = (await session.scalars(select(CrashJob))).all()
                if not remaining:
                    break
            await asyncio.sleep(0.05)

        assert len(msdl_http_calls) == 1
        assert len(stackwalk_runs) == 2
        assert has_cached_symbol(storage_manager, "ntdll.pdb", NTDLL_DEBUG_ID)

        async with factory() as session:
            crash = await session.get(CrashReport, crash_id)
            assert crash is not None
            assert crash.status == "ANALYZED"
            assert crash.missing_symbols_json == []
            frame0 = crash.parsed_stacks_json["threads"][0]["frames"][0]
            assert frame0["function"] == "KiUserExceptionDispatcher"
    finally:
        await dispatcher.drain_and_cancel()
        await engine.dispose()


async def test_fetch_ms_symbols_preserves_existing_ai_run_and_skips_active_crash(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'msdl_ai.db'}"
    engine = create_async_engine_from_url(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    settings = test_settings.model_copy(
        update={"DATABASE_URL": database_url, "MS_SYMBOL_FETCH_PE_UNWIND": False}
    )

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            project = Project(key="ai-guard", name="AI Guard", api_key_hash="hash")
            session.add(project)
            await session.flush()
            crash_id = uuid.uuid4()
            raw_dmp = tmp_path / "ai.dmp"
            raw_dmp.write_bytes(b"MDMP_AI")
            dump_path, _, _ = storage_manager.compress_dump(raw_dmp, crash_id)
            session.add(
                CrashReport(
                    id=crash_id,
                    project_id=project.id,
                    status="ANALYZED",
                    app_version="1.0.0",
                    dump_path=dump_path,
                )
            )
            session.add(
                AIAnalysisRun(
                    crash_id=crash_id,
                    model_name="gpt-4o-mini",
                    status="COMPLETED",
                )
            )
            await session.commit()

        ctx = {
            "settings": settings,
            "session_factory": factory,
            "storage": storage_manager,
            "event_bus": InMemoryEventBus(),
            "exec_fn": make_mock_exec(
                returncode=0, stdout=_sample_sym("ntdll.pdb", NTDLL_DEBUG_ID)
            ),
            "http_transport": httpx.MockTransport(
                lambda _req: httpx.Response(200, content=FAKE_PDB_BYTES)
            ),
        }
        cand = WindowsSymbolCandidate(
            module_name="ntdll.dll",
            debug_file="ntdll.pdb",
            debug_id=NTDLL_DEBUG_ID,
            filename=r"C:\Windows\System32\ntdll.dll",
        )
        res = await fetch_ms_symbols(ctx, crash_id, project.id, [cand.to_dict()])
        assert res["imported"] == 1
        assert res["resymbolicate_queued"] is True

        async with factory() as session:
            job = (await session.scalars(select(CrashJob))).one()
            assert job.task == "resymbolicate"
            # Existing AIAnalysisRun prevents re-spending LLM tokens on auto-resymbolicate.
            assert job.run_ai_override is False
    finally:
        await engine.dispose()


async def test_explicit_force_sync_bypasses_allowlist_normalizes_urls_and_upgrades_pe(
    storage_manager: StorageManager,
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    from app.db.models import Symbol

    gc_did = "90D28435EAAB109449C47402C53C42D31"
    raw_code_id = "65A1B2C30001A000"
    canonical_code_id = "65A1B2C31a000"
    requested_paths: list[str] = []
    serve_pe = {"enabled": False}

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path.endswith(
            f"/graphicscapture.pdb/{gc_did}/graphicscapture.pdb"
        ):
            return httpx.Response(200, content=FAKE_PDB_BYTES)
        if serve_pe["enabled"] and request.url.path.endswith(
            f"/graphicscapture.dll/{canonical_code_id}/graphicscapture.dll"
        ):
            return httpx.Response(200, content=FAKE_PE_BYTES)
        return httpx.Response(404)

    async def fake_dump_syms(*cmd: str, **_kwargs: Any) -> Any:
        has_pe = len(cmd) >= 3
        out = _sample_sym(
            "GraphicsCapture.pdb",
            gc_did,
            canonical_code_id if has_pe else None,
        )

        class _Proc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return out, b""

            def kill(self) -> None:
                pass

        return _Proc()

    cand = parse_manual_ms_target(
        "GraphicsCapture.dll",
        gc_did,
        code_id=raw_code_id,
    )
    # 1. Initial sync without PE imports PDB-only and stores global Symbol row
    s1 = await fetch_and_import_windows_symbols(
        [cand],
        storage_manager,
        test_settings.model_copy(update={"MS_SYMBOL_AUTO_FETCH": False}),
        session=db_session,
        explicit_targets=True,
        force=True,
        bypass_auto_fetch_flag=True,
        exec_fn=fake_dump_syms,
        http_transport=httpx.MockTransport(handler),
    )
    assert s1.imported == 1
    assert s1.items[0].status == "imported"
    assert s1.items[0].has_pe_unwind is False
    assert any(
        p.endswith(f"/graphicscapture.pdb/{gc_did}/graphicscapture.pdb")
        for p in requested_paths
    )
    assert any(
        p.endswith(f"/graphicscapture.dll/{canonical_code_id}/graphicscapture.dll")
        for p in requested_paths
    )

    found, scope, _ = has_cached_symbol_detail(
        storage_manager, "GraphicsCapture.pdb", gc_did
    )
    assert found is True
    assert scope == "public"

    row = (
        await db_session.scalars(
            select(Symbol).where(Symbol.debug_id == gc_did)
        )
    ).one()
    assert row.project_id is None
    assert row.app_version is None
    assert row.source == "msdl"
    assert row.has_pe_unwind is False

    # 2. Second force sync once PE is available upgrades existing public symbol
    serve_pe["enabled"] = True
    s2 = await fetch_and_import_windows_symbols(
        [cand],
        storage_manager,
        test_settings,
        session=db_session,
        explicit_targets=True,
        force=True,
        bypass_auto_fetch_flag=True,
        exec_fn=fake_dump_syms,
        http_transport=httpx.MockTransport(handler),
    )
    assert s2.upgraded == 1
    assert s2.items[0].status == "upgraded"
    assert s2.items[0].has_pe_unwind is True
    await db_session.refresh(row)
    assert row.has_pe_unwind is True


async def test_import_preserves_requested_casing_callbacks_and_force_circuit(
    storage_manager: StorageManager,
    test_settings: Settings,
) -> None:
    did = "07672F300BBD98B7671A8D52A55C973E1"
    started: list[str] = []
    finished: list[tuple[str, str]] = []

    def ok_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/audioses.pdb/{did}/audioses.pdb"):
            return httpx.Response(200, content=FAKE_PDB_BYTES)
        return httpx.Response(404)

    # dump_syms emits lowercase audioses.pdb even though candidate requested AudioSes.pdb
    cand = WindowsSymbolCandidate(
        module_name="AudioSes.dll",
        debug_file="AudioSes.pdb",
        debug_id=did,
        code_file="AudioSes.dll",
    )
    settings = test_settings.model_copy(update={"MS_SYMBOL_FETCH_PE_UNWIND": False})
    summary = await fetch_and_import_windows_symbols(
        [cand],
        storage_manager,
        settings,
        force=True,
        bypass_auto_fetch_flag=True,
        on_item_start=lambda c: started.append(c.debug_file),
        on_item_done=lambda it: finished.append((it.debug_file, it.status)),
        exec_fn=make_mock_exec(returncode=0, stdout=_sample_sym("audioses.pdb", did)),
        http_transport=httpx.MockTransport(ok_handler),
    )
    assert summary.imported == 1
    assert started == ["AudioSes.pdb"]
    assert finished == [("AudioSes.pdb", "imported")]
    expected_exact = (
        storage_manager.partition_path("symbols/public_cache")
        / "AudioSes.pdb"
        / did
        / "AudioSes.sym"
    )
    assert expected_exact.is_file()

    # Force batch with sustained outage should trip circuit fresh and short-circuit remaining items
    offline_calls = 0

    def offline_handler(req: httpx.Request) -> httpx.Response:
        nonlocal offline_calls
        offline_calls += 1
        raise httpx.ConnectError("network down", request=req)

    offline_cands = [
        WindowsSymbolCandidate(
            module_name="ntdll.dll",
            debug_file="ntdll.pdb",
            debug_id=f"{idx:032X}1",
            code_file="ntdll.dll",
        )
        for idx in range(10, 17)
    ]
    done_statuses: list[str] = []
    offline_summary = await fetch_and_import_windows_symbols(
        offline_cands,
        storage_manager,
        settings,
        force=True,
        bypass_auto_fetch_flag=True,
        max_concurrency=1,
        on_item_done=lambda it: done_statuses.append(it.status),
        http_transport=httpx.MockTransport(offline_handler),
    )
    assert offline_summary.circuit_open is True
    assert offline_summary.failed == CIRCUIT_FAILURE_THRESHOLD
    assert offline_summary.skipped == len(offline_cands) - CIRCUIT_FAILURE_THRESHOLD
    assert len(done_statuses) == len(offline_cands)
    assert is_circuit_open(storage_manager)

    # A subsequent force=True retry clears the stale circuit and succeeds when network recovers
    recover_cand = WindowsSymbolCandidate(
        module_name="kernel32.dll",
        debug_file="kernel32.pdb",
        debug_id=KERNELBASE_DEBUG_ID,
        code_file="kernel32.dll",
    )
    recovered = await fetch_and_import_windows_symbols(
        [recover_cand],
        storage_manager,
        settings,
        force=True,
        bypass_auto_fetch_flag=True,
        exec_fn=make_mock_exec(
            returncode=0, stdout=_sample_sym("kernel32.pdb", KERNELBASE_DEBUG_ID)
        ),
        http_transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=FAKE_PDB_BYTES)),
    )
    assert recovered.imported == 1
    assert not is_circuit_open(storage_manager)


async def test_force_sync_cached_pdb_without_pe_returns_cached_hit_not_upgraded(
    storage_manager: StorageManager,
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    gc_did = "90D28435EAAB109449C47402C53C42D31"
    raw_code_id = "65A1B2C30001A000"
    canonical_code_id = "65A1B2C31a000"
    serve_pe = {"enabled": False}
    dump_syms_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/graphicscapture.pdb/{gc_did}/graphicscapture.pdb"):
            return httpx.Response(200, content=FAKE_PDB_BYTES)
        if serve_pe["enabled"] and request.url.path.endswith(
            f"/graphicscapture.dll/{canonical_code_id}/graphicscapture.dll"
        ):
            return httpx.Response(200, content=FAKE_PE_BYTES)
        return httpx.Response(404)

    async def counting_dump_syms(*cmd: str, **_kwargs: Any) -> Any:
        nonlocal dump_syms_calls
        dump_syms_calls += 1
        has_pe = len(cmd) >= 3
        out = _sample_sym(
            "GraphicsCapture.pdb",
            gc_did,
            canonical_code_id if has_pe else None,
        )

        class _Proc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return out, b""

            def kill(self) -> None:
                pass

        return _Proc()

    cand = parse_manual_ms_target("GraphicsCapture.dll", gc_did, code_id=raw_code_id)

    # 1. First sync imports PDB-only (PE 404)
    s1 = await fetch_and_import_windows_symbols(
        [cand],
        storage_manager,
        test_settings,
        session=db_session,
        explicit_targets=True,
        force=True,
        bypass_auto_fetch_flag=True,
        exec_fn=counting_dump_syms,
        http_transport=httpx.MockTransport(handler),
    )
    assert s1.imported == 1
    assert s1.upgraded == 0
    assert s1.items[0].has_pe_unwind is False
    assert dump_syms_calls == 1

    # 2. Second force sync when PE is STILL 404 must report cached_hit (NOT fake 'upgraded')
    # and skip redundant dump_syms conversion.
    s2 = await fetch_and_import_windows_symbols(
        [cand],
        storage_manager,
        test_settings,
        session=db_session,
        explicit_targets=True,
        force=True,
        bypass_auto_fetch_flag=True,
        exec_fn=counting_dump_syms,
        http_transport=httpx.MockTransport(handler),
    )
    assert s2.upgraded == 0
    assert s2.imported == 0
    assert s2.cached_hit == 1
    assert s2.items[0].status == "cached_hit"
    assert s2.items[0].has_pe_unwind is False
    assert dump_syms_calls == 1

    # 3. Third force sync once PE IS available genuinely upgrades with PE unwind
    serve_pe["enabled"] = True
    s3 = await fetch_and_import_windows_symbols(
        [cand],
        storage_manager,
        test_settings,
        session=db_session,
        explicit_targets=True,
        force=True,
        bypass_auto_fetch_flag=True,
        exec_fn=counting_dump_syms,
        http_transport=httpx.MockTransport(handler),
    )
    assert s3.upgraded == 1
    assert s3.items[0].status == "upgraded"
    assert s3.items[0].has_pe_unwind is True
    assert dump_syms_calls > 1
