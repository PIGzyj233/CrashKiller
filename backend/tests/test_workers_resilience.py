"""Unit tests for worker isolation, expire_on_commit safety, and edge cases."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import CrashReport, Project
from app.services.events import (
    InMemoryEventBus,
)
from app.services.storage import StorageManager
from app.workers import (
    ai_triage,
    parse_crash,
    resymbolicate,
    symbol_ingest,
)
from tests._worker_test_helpers import _sample_stackwalk_json
from tests.conftest import make_mock_exec


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
