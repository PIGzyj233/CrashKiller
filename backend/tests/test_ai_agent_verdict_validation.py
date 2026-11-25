"""Unit tests for AITriageAgent verdict validation, confidence guards, and concurrency."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import (
    CrashCluster,
    CrashReport,
    Issue,
    Project,
)
from app.services.ai_agent import (
    AITriageAgent,
    _parse_verdict_json,
)
from app.services.storage import StorageManager
from tests._ai_agent_helpers import (
    FakeOpenAIClient,
    _investigate_then_verdict,
    _make_response,
    _seed_crash_with_log,
)
from tests.conftest import make_disk_usage_fn


async def test_guardrail_max_steps_timeout_truncation_and_malformed_json(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    _proj, crash, log_row = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path, log_lines_count=40
    )
    assert log_row is not None
    agent = AITriageAgent(storage=storage_manager, settings=test_settings)

    # 1. Max steps exceeded -> NEEDS_REVIEW + trace_json <= 3.
    loop_client = FakeOpenAIClient(
        lambda _s, _k: _make_response(
            tool_calls=[{"id": "loop", "name": "get_crash_overview", "arguments": {}}]
        )
    )
    run_steps = await agent.run_triage(
        db_session,
        crash,
        settings_override={"ai_max_steps": 2},
        openai_client=loop_client,
    )
    assert run_steps.status == "FAILED"
    assert run_steps.result_json is not None
    assert run_steps.result_json["verdict"] == "NEEDS_REVIEW"
    assert len(run_steps.trace_json) <= 3
    await db_session.refresh(crash)
    assert crash.status == "ANALYZED"
    assert crash.ai_verdict == "NEEDS_REVIEW"

    # 2. Timeout -> NEEDS_REVIEW.
    async def slow_handler(_s: int, _k: dict[str, Any]) -> Any:
        await asyncio.sleep(2.0)
        return _make_response(content='{"verdict": "NEW_ISSUE"}')

    run_timeout = await agent.run_triage(
        db_session,
        crash,
        settings_override={"ai_timeout_seconds": 1},
        openai_client=FakeOpenAIClient(slow_handler),
    )
    assert run_timeout.status == "FAILED"
    assert run_timeout.result_json["verdict"] == "NEEDS_REVIEW"

    # 3. Tool output truncation strictly within ai_tool_output_max_chars=300.
    def trunc_handler(step: int, _k: dict[str, Any]) -> Any:
        if step == 1:
            return _make_response(
                tool_calls=[
                    {
                        "id": "r1",
                        "name": "read_log_lines",
                        "arguments": {"log_id": str(log_row.id), "start_line": 1, "end_line": 40},
                    }
                ]
            )
        return _make_response(content='{"verdict": "NEEDS_REVIEW", "confidence": 0.2}')

    trunc_client = FakeOpenAIClient(trunc_handler)
    await agent.run_triage(
        db_session,
        crash,
        settings_override={"ai_tool_output_max_chars": 300},
        openai_client=trunc_client,
    )
    tool_msg = next(m for m in trunc_client.calls[1]["messages"] if m["role"] == "tool")
    assert len(tool_msg["content"]) <= 300
    assert tool_msg["content"].endswith("...[TRUNCATED]")

    # 4. Missing API key without injected client -> immediate NEEDS_REVIEW without HTTP call.
    run_nokey = await agent.run_triage(db_session, crash, openai_client=None)
    assert run_nokey.status == "FAILED"
    assert run_nokey.result_json["verdict"] == "NEEDS_REVIEW"

    # 5. Malformed JSON -> safe degradation to NEEDS_REVIEW.
    bad_json_client = FakeOpenAIClient(lambda _s, _k: _make_response(content="not json {{"))
    run_bad = await agent.run_triage(db_session, crash, openai_client=bad_json_client)
    assert run_bad.status == "FAILED"
    assert run_bad.result_json["verdict"] == "NEEDS_REVIEW"


async def test_verdict_non_finite_confidence_guarded(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """NaN and Inf confidence values must be guarded to finite [0.0, 1.0] values."""
    for raw_conf in ("NaN", "Infinity", "-Infinity"):
        parsed = _parse_verdict_json(
            f'{{"verdict": "NEW_ISSUE", "confidence": {raw_conf}, "suggested_title": "T"}}'
        )
        assert math.isfinite(parsed["confidence"])
        assert 0.0 <= parsed["confidence"] <= 1.0

    for str_conf in ("nan", "inf", "-inf"):
        parsed = _parse_verdict_json(
            json.dumps({"verdict": "NEW_ISSUE", "confidence": str_conf, "suggested_title": "T"})
        )
        assert math.isfinite(parsed["confidence"])
        assert 0.0 <= parsed["confidence"] <= 1.0

    _proj, crash, _ = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path, with_log=False
    )
    nan_client = FakeOpenAIClient(
        _investigate_then_verdict(
            '{"verdict": "NEW_ISSUE", "confidence": NaN, "suggested_title": "NaN Conf", '
            '"root_cause_analysis": "Null dereference in SubmitDraw."}'
        )
    )
    agent = AITriageAgent(storage=storage_manager, settings=test_settings)
    run = await agent.run_triage(db_session, crash, openai_client=nan_client)
    assert run.status == "COMPLETED"
    await db_session.refresh(crash)
    assert crash.ai_confidence is not None
    assert math.isfinite(crash.ai_confidence)
    assert 0.0 <= crash.ai_confidence <= 1.0


async def test_verdict_boolean_confidence_rejected_and_expire_on_commit_refreshes_crash(
    async_engine: AsyncEngine,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """Boolean confidence must raise ValueError / degrade to NEEDS_REVIEW, and run_triage
    must refresh crash_report so attributes are readable under expire_on_commit=True.
    """
    for raw_bool in ("true", "false"):
        with pytest.raises(ValueError, match="boolean"):
            _parse_verdict_json(
                f'{{"verdict": "NEW_ISSUE", "confidence": {raw_bool}, "suggested_title": "T"}}'
            )

    exp_factory = async_sessionmaker(
        bind=async_engine, class_=AsyncSession, expire_on_commit=True, autoflush=False
    )
    async with exp_factory() as exp_session:
        _proj, crash_bool, _ = await _seed_crash_with_log(
            exp_session, storage_manager, tmp_path, with_log=False
        )
        await exp_session.refresh(crash_bool)
        bool_conf_client = FakeOpenAIClient(
            lambda _s, _k: _make_response(
                content='{"verdict": "NEW_ISSUE", "confidence": true, "suggested_title": "Bool"}'
            )
        )
        agent = AITriageAgent(storage=storage_manager, settings=test_settings)
        run_bad = await agent.run_triage(exp_session, crash_bool, openai_client=bool_conf_client)
        assert run_bad.status == "FAILED"
        # Readable directly without caller-side session.refresh(crash_bool):
        assert crash_bool.status == "ANALYZED"
        assert crash_bool.ai_verdict == "NEEDS_REVIEW"
        assert crash_bool.ai_confidence == 0.0

        _proj2, crash_ok, _ = await _seed_crash_with_log(
            exp_session, storage_manager, tmp_path, with_log=False
        )
        await exp_session.refresh(crash_ok)
        ok_client = FakeOpenAIClient(
            _investigate_then_verdict(
                json.dumps(
                    {
                        "verdict": "NEW_ISSUE",
                        "confidence": 0.87,
                        "suggested_title": "Expire Refresh Crash",
                        "root_cause_analysis": "Null pointer",
                    }
                )
            )
        )
        run_ok = await agent.run_triage(exp_session, crash_ok, openai_client=ok_client)
        assert run_ok.status == "COMPLETED"
        # Readable directly without caller-side session.refresh(crash_ok):
        assert crash_ok.status == "ANALYZED"
        assert crash_ok.ai_verdict == "NEW_ISSUE"
        assert crash_ok.ai_confidence == 0.87
        assert crash_ok.issue_id is not None


@pytest.mark.parametrize("same_report", [True, False], ids=["same-report", "same-cluster"])
async def test_finalize_run_concurrent_verdicts_share_one_issue(
    tmp_data_dir: Path,
    tmp_path: Path,
    same_report: bool,
) -> None:
    """Competing AI completions leave one issue for the first reports in a cluster."""
    from app.db.base import Base
    from app.db.session import create_async_engine_from_url

    db_file = tmp_path / "race_finalize.db"
    engine = create_async_engine_from_url(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    settings = Settings(
        DATABASE_URL=f"sqlite+aiosqlite:///{db_file}",
        DATA_DIR=str(tmp_data_dir),
    )
    mgr = StorageManager(
        data_dir=tmp_data_dir,
        settings=settings,
        disk_usage_fn=make_disk_usage_fn(free_gb=200.0),
    )
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    try:
        async with factory() as seed_sess:
            proj = Project(key="race", name="Race", api_key_hash="k")
            seed_sess.add(proj)
            await seed_sess.flush()

            fixed_issue = Issue(
                project_id=proj.id,
                title="Previously Fixed Issue",
                status="FIXED",
                fixed_in_version="1.0.0",
                crash_count=0,
                is_ambiguous_stack=False,
                log_patterns_json=[],
            )
            seed_sess.add(fixed_issue)
            await seed_sess.flush()

            report_count = 1 if same_report else 2
            cluster = CrashCluster(
                project_id=proj.id,
                issue_id=None,
                signature_hash=hashlib.sha256(b"race-sig").hexdigest(),
                signature_frames_json=[{"module": "app.exe", "function": "RaceFn()"}],
                signature_text="app.exe!RaceFn()",
                exception_type="EXCEPTION_ACCESS_VIOLATION_READ",
                crash_count=report_count,
            )
            seed_sess.add(cluster)
            await seed_sess.flush()

            reports = [
                CrashReport(
                    project_id=proj.id,
                    cluster_id=cluster.id,
                    status="TRIAGING",
                    app_version="2.0.0",
                    crash_reason="EXCEPTION_ACCESS_VIOLATION_READ",
                    parsed_stacks_json={"threads": [{"thread_index": 0, "frames": []}]},
                    crash_time=datetime.now(UTC),
                )
                for _ in range(report_count)
            ]
            seed_sess.add_all(reports)
            await seed_sess.commit()
            crash_id = reports[0].id
            competing_crash_id = reports[-1].id
            cluster_id = cluster.id
            fixed_issue_id = fixed_issue.id

        agent = AITriageAgent(storage=mgr, settings=settings)

        async with factory() as s1, factory() as s2:
            c1 = await s1.get(CrashReport, crash_id)
            c2 = await s2.get(CrashReport, competing_crash_id)
            assert c1 is not None and c2 is not None

            entered_refresh = asyncio.Event()
            allow_s2_resume = asyncio.Event()
            orig_refresh = s2.refresh

            async def _paused_refresh(instance: Any, *args: Any, **kwargs: Any) -> Any:
                res = await orig_refresh(instance, *args, **kwargs)
                if isinstance(instance, CrashCluster):
                    entered_refresh.set()
                    await asyncio.wait_for(allow_s2_resume.wait(), timeout=5)
                return res

            s2.refresh = _paused_refresh  # type: ignore[method-assign]

            competing_task = asyncio.create_task(
                agent._finalize_run(
                    session=s2,
                    crash_report=c2,
                    model_name="mock",
                    run_status="COMPLETED",
                    result_dict={
                        "verdict": "KNOWN_ISSUE" if same_report else "NEW_ISSUE",
                        "matched_issue_id": fixed_issue_id if same_report else None,
                        "suggested_title": "Losing New Issue",
                        "confidence": 0.9,
                        "key_log_patterns": ["PAT_X"],
                    },
                    trace=[],
                    prompt_tokens=10,
                    completion_tokens=10,
                    duration_ms=5,
                )
            )
            await asyncio.wait_for(entered_refresh.wait(), timeout=5)

            # Run 1 (NEW_ISSUE) commits first while Run 2 is paused after cluster refresh
            await agent._finalize_run(
                session=s1,
                crash_report=c1,
                model_name="mock",
                run_status="COMPLETED",
                result_dict={
                    "verdict": "NEW_ISSUE",
                    "matched_issue_id": None,
                    "confidence": 0.85,
                    "suggested_title": "Winning New Issue",
                    "key_log_patterns": [],
                },
                trace=[],
                prompt_tokens=10,
                completion_tokens=10,
                duration_ms=5,
            )

            allow_s2_resume.set()
            await competing_task

        async with factory() as verify_sess:
            final_crash = await verify_sess.get(CrashReport, crash_id)
            final_cluster = await verify_sess.get(CrashCluster, cluster_id)
            final_fixed = await verify_sess.get(Issue, fixed_issue_id)
            assert final_crash is not None and final_cluster is not None and final_fixed is not None

            assert final_crash.issue_id is not None
            assert final_crash.issue_id != fixed_issue_id
            assert final_cluster.issue_id == final_crash.issue_id
            assert final_crash.ai_verdict == "NEW_ISSUE"
            other_report = await verify_sess.get(CrashReport, competing_crash_id)
            assert other_report.issue_id == final_crash.issue_id
            winning_issue = await verify_sess.get(Issue, final_crash.issue_id)
            assert winning_issue.crash_count == report_count
            assert len((await verify_sess.scalars(select(Issue))).all()) == 2
            assert final_fixed.status == "FIXED"
            assert final_fixed.crash_count == 0
            assert final_fixed.log_patterns_json == []
    finally:
        await engine.dispose()
