"""Comprehensive unit tests for AITriageAgent ReAct loop, 8 tools, and guardrails."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import (
    CrashCluster,
    CrashLog,
    CrashReport,
    Issue,
    Project,
    SystemSetting,
)
from app.services.ai_agent import (
    AI_TOOL_DEFINITIONS,
    AITriageAgent,
    _parse_verdict_json,
    _prepare_tool_output_for_llm,
    run_crash_triage,
)
from app.services.clustering import cluster_crash
from app.services.log_engine import LogEngine
from app.services.storage import StorageManager
from tests.conftest import make_disk_usage_fn


def _make_response(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    prompt_tokens: int = 100,
    completion_tokens: int = 40,
) -> SimpleNamespace:
    tc_objs = None
    if tool_calls:
        tc_objs = [
            SimpleNamespace(
                id=tc.get("id", f"call_{i}"),
                type="function",
                function=SimpleNamespace(
                    name=tc["name"],
                    arguments=json.dumps(tc.get("arguments", {})),
                ),
            )
            for i, tc in enumerate(tool_calls, start=1)
        ]
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    tool_calls=tc_objs,
                )
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


def _investigate_then_verdict(content: str) -> Any:
    """Return a mock handler that calls get_crash_overview before emitting a verdict."""

    def _handler(_step: int, kwargs: dict[str, Any]) -> Any:
        has_tool_result = any(
            isinstance(m, dict) and m.get("role") == "tool"
            for m in (kwargs.get("messages") or [])
        )
        if not has_tool_result:
            return _make_response(
                tool_calls=[{"id": "ov1", "name": "get_crash_overview", "arguments": {}}]
            )
        return _make_response(content=content)

    return _handler


class FakeOpenAIClient:
    """Scripted mock OpenAI async client capturing outgoing messages."""

    def __init__(self, handler: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self._handler = handler

        async def _create(**kwargs: Any) -> Any:
            self.calls.append(
                {
                    "model": kwargs.get("model"),
                    "messages": json.loads(json.dumps(kwargs.get("messages", []))),
                    "tools": kwargs.get("tools"),
                }
            )
            res = self._handler(len(self.calls), kwargs)
            if asyncio.iscoroutine(res):
                return await res
            return res

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))


async def _seed_crash_with_log(
    session: AsyncSession,
    storage: StorageManager,
    tmp_path: Path,
    *,
    app_version: str = "1.0.0",
    with_log: bool = True,
    log_lines_count: int = 30,
) -> tuple[Project, CrashReport, CrashLog | None]:
    proj = Project(
        key=f"ai-proj-{uuid.uuid4().hex[:6]}",
        name="AI Test Project",
        api_key_hash=hashlib.sha256(b"key").hexdigest(),
        settings_json={},
    )
    session.add(proj)
    await session.flush()

    crash_time = datetime(2026, 11, 22, 10, 0, 5, tzinfo=UTC)
    stacks = {
        "crashing_thread_index": 0,
        "threads": [
            {
                "thread_index": 0,
                "thread_id": 100,
                "thread_name": "MainThread",
                "frames": [
                    {
                        "frame_index": 0,
                        "module": "Renderer.dll",
                        "function": "Renderer::SubmitDraw(int)",
                        "module_offset": "0x1200",
                        "file": "renderer.cpp",
                        "line": 128,
                        "missing_symbols": False,
                        "inlines": [],
                    }
                ],
            },
            {
                "thread_index": 1,
                "thread_id": 200,
                "thread_name": "WorkerIOThread",
                "frames": [
                    {
                        "frame_index": 0,
                        "module": "IO.dll",
                        "function": "DiskCache::Flush()",
                        "module_offset": "0x500",
                        "file": "disk_cache.cpp",
                        "line": 44,
                        "missing_symbols": False,
                        "inlines": [],
                    }
                ],
            },
        ],
        "modules": [],
    }

    crash = CrashReport(
        project_id=proj.id,
        status="TRIAGING",
        app_version=app_version,
        crash_reason="EXCEPTION_ACCESS_VIOLATION_READ",
        crash_address="0x0",
        parsed_summary_json={"top_frames": stacks["threads"][0]["frames"]},
        parsed_stacks_json=stacks,
        missing_symbols_json=[],
        crash_time=crash_time,
    )
    session.add(crash)
    await session.flush()

    await cluster_crash(session, crash, proj, triage_mode="ALWAYS")

    log_row: CrashLog | None = None
    if with_log:
        log_id = uuid.uuid4()
        raw_log = tmp_path / f"raw_{log_id.hex}.log"
        lines_bytes: list[bytes] = []
        for idx in range(1, log_lines_count + 1):
            ts_sec = min(59, idx)
            if idx == 5:
                lines_bytes.append(
                    (
                        f"2026-11-22T10:00:{ts_sec:02d}Z ERROR "
                        f"Renderer::SubmitDraw null texture pointer verbose_line_{idx}\n"
                    ).encode()
                )
            elif idx == 6:
                lines_bytes.append(
                    f"2026-11-22T10:00:{ts_sec:02d}Z FATAL 渲染纹理为空 line_{idx}\n".encode("gbk")
                )
            else:
                lines_bytes.append(
                    (
                        f"2026-11-22T10:00:{ts_sec:02d}Z INFO "
                        f"Frame tick verbose_line_{idx} " + ("x" * 20) + "\n"
                    ).encode("utf-8")
                )
        raw_log.write_bytes(b"".join(lines_bytes))

        rel_zst = f"logs/{crash.id}/{log_id}.log.zst"
        rel_idx = f"logs/{crash.id}/{log_id}.idx.json"
        abs_zst = storage.resolve_path(rel_zst)
        abs_idx = storage.resolve_path(rel_idx)
        abs_zst.parent.mkdir(parents=True, exist_ok=True)
        ingest_info = LogEngine().ingest_log(raw_log, abs_zst, abs_idx)

        log_row = CrashLog(
            id=log_id,
            crash_id=crash.id,
            filename="app.log",
            zst_path=rel_zst,
            idx_path=rel_idx,
            raw_size=ingest_info["raw_size"],
            compressed_size=ingest_info["compressed_size"],
            total_lines=ingest_info["total_lines"],
            digest_json=ingest_info["digest_json"],
        )
        session.add(log_row)

    await session.commit()
    return proj, crash, log_row


async def test_react_multiturn_all_tools_and_trace(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    assert len(AI_TOOL_DEFINITIONS) == 8
    proj, crash, log_row = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path, log_lines_count=260
    )
    assert log_row is not None

    known_issue = Issue(
        project_id=proj.id,
        title="Null texture in Renderer::SubmitDraw",
        status="KNOWN",
        component="Renderer",
        root_cause="Missing null guard on texture handle",
        crash_count=2,
    )
    db_session.add(known_issue)
    await db_session.commit()

    def handler(step: int, _kwargs: dict[str, Any]) -> Any:
        if step == 1:
            return _make_response(
                tool_calls=[
                    {"id": "t1", "name": "get_crash_overview", "arguments": {}},
                    {"id": "t2", "name": "get_other_threads", "arguments": {"keyword": "Worker"}},
                ]
            )
        if step == 2:
            return _make_response(
                tool_calls=[
                    {
                        "id": "t3",
                        "name": "get_log_summary",
                        "arguments": {"log_id": str(log_row.id)},
                    },
                    {
                        "id": "t4",
                        "name": "read_log_lines",
                        "arguments": {"log_id": str(log_row.id), "start_line": 1, "end_line": 250},
                    },
                    {
                        "id": "t5",
                        "name": "read_log_around_time",
                        "arguments": {"log_id": str(log_row.id), "window_seconds": 10},
                    },
                    {
                        "id": "t6",
                        "name": "search_log",
                        "arguments": {"log_id": str(log_row.id), "pattern": "FATAL"},
                    },
                    {
                        "id": "t7",
                        "name": "search_known_issues",
                        "arguments": {"query": ""},
                    },
                    {
                        "id": "t8",
                        "name": "get_issue_detail",
                        "arguments": {"issue_id": known_issue.id},
                    },
                ]
            )
        return _make_response(
            content=json.dumps(
                {
                    "verdict": "KNOWN_ISSUE",
                    "matched_issue_id": known_issue.id,
                    "confidence": 0.93,
                    "suggested_title": "Null texture in Renderer::SubmitDraw",
                    "root_cause_analysis": "Log confirms null texture pointer at line 5.",
                    "reasoning_summary": "Matched stack and FATAL log signature.",
                    "evidence_log_snippets": [
                        {
                            "log_id": str(log_row.id),
                            "start_line": 5,
                            "end_line": 6,
                            "relevance": "Fatal error",
                        }
                    ],
                    "key_log_patterns": ["null texture pointer"],
                    "suggested_component": "Renderer",
                }
            )
        )

    client = FakeOpenAIClient(handler)
    run = await run_crash_triage(
        db_session,
        crash,
        storage_manager,
        settings=test_settings,
        settings_override={"ai_tool_output_max_chars": 100000},
        openai_client=client,
    )

    assert run.status == "COMPLETED"
    assert run.prompt_tokens == 300
    assert run.completion_tokens == 120
    assert run.duration_ms >= 0
    assert len(run.trace_json) == 8

    # Inspect tool outputs returned to the LLM across steps 2 and 3.
    step2_tool_msgs = {
        m["tool_call_id"]: json.loads(m["content"])
        for m in client.calls[1]["messages"]
        if m["role"] == "tool"
    }
    assert step2_tool_msgs["t1"]["logs"][0]["id"] == str(log_row.id)
    assert step2_tool_msgs["t2"]["threads"][0]["thread_name"] == "WorkerIOThread"

    step3_tool_msgs = {
        m["tool_call_id"]: json.loads(m["content"])
        for m in client.calls[2]["messages"]
        if m["role"] == "tool"
    }
    assert step3_tool_msgs["t4"]["capped_to_200"] is True
    assert len(step3_tool_msgs["t4"]["lines"]) == 200
    assert step3_tool_msgs["t6"]["total_matches"] >= 1
    assert any(it["issue_id"] == known_issue.id for it in step3_tool_msgs["t7"]["issues"])
    assert step3_tool_msgs["t8"]["issue_id"] == known_issue.id

    await db_session.refresh(crash)
    await db_session.refresh(known_issue)
    assert crash.status == "ANALYZED"
    assert crash.ai_verdict == "KNOWN_ISSUE"
    assert crash.issue_id == known_issue.id
    assert known_issue.crash_count == 3
    assert "null texture pointer" in known_issue.log_patterns_json


async def test_verdict_new_issue_creates_issue_and_links_cluster(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    _proj, crash, _ = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path, with_log=False
    )

    handler = _investigate_then_verdict(
        json.dumps(
            {
                "verdict": "NEW_ISSUE",
                "matched_issue_id": None,
                "confidence": 0.88,
                "suggested_title": "Crash in SubmitDraw",
                "root_cause_analysis": "Dereferenced null context",
                "reasoning_summary": "No matching issue found.",
                "evidence_log_snippets": [],
                "key_log_patterns": ["SubmitDraw null"],
                "suggested_component": "Renderer",
            }
        )
    )

    agent = AITriageAgent(storage=storage_manager, settings=test_settings)
    run = await agent.run_triage(db_session, crash, openai_client=FakeOpenAIClient(handler))

    assert run.status == "COMPLETED"
    await db_session.refresh(crash)
    assert crash.ai_verdict == "NEW_ISSUE"
    assert crash.issue_id is not None
    assert crash.status == "ANALYZED"

    cluster = await db_session.get(CrashCluster, crash.cluster_id)
    assert cluster is not None
    assert cluster.issue_id == crash.issue_id
    new_issue = await db_session.get(Issue, crash.issue_id)
    assert new_issue is not None
    assert new_issue.status == "NEW"
    assert new_issue.title == "Crash in SubmitDraw"


async def test_verdict_regression_on_fixed_issue_version_comparison(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    proj, crash_old, _ = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path, app_version="1.1.5", with_log=False
    )
    fixed_issue = Issue(
        project_id=proj.id,
        title="Fixed renderer bug",
        status="FIXED",
        fixed_in_version="1.2.0",
        crash_count=1,
    )
    db_session.add(fixed_issue)
    await db_session.commit()

    # If LLM says REGRESSION on pre-fix build 1.1.5, downgrade to KNOWN_ISSUE and keep FIXED.
    handler_old = _investigate_then_verdict(
        json.dumps(
            {
                "verdict": "REGRESSION",
                "matched_issue_id": fixed_issue.id,
                "confidence": 0.8,
                "suggested_title": "Fixed renderer bug",
                "root_cause_analysis": "Same stack",
                "reasoning_summary": "Matched",
            }
        )
    )

    agent = AITriageAgent(storage=storage_manager, settings=test_settings)
    await agent.run_triage(db_session, crash_old, openai_client=FakeOpenAIClient(handler_old))
    await db_session.refresh(crash_old)
    await db_session.refresh(fixed_issue)
    assert crash_old.ai_verdict == "KNOWN_ISSUE"
    assert fixed_issue.status == "FIXED"

    # On post-fix build 1.10.0 (> 1.2.0), even if LLM says KNOWN_ISSUE, code promotes to REGRESSION.
    crash_new = CrashReport(
        project_id=proj.id,
        cluster_id=crash_old.cluster_id,
        status="TRIAGING",
        app_version="1.10.0",
        crash_reason="EXCEPTION_ACCESS_VIOLATION_READ",
        parsed_stacks_json=crash_old.parsed_stacks_json,
    )
    db_session.add(crash_new)
    await db_session.commit()

    handler_new = _investigate_then_verdict(
        json.dumps(
            {
                "verdict": "KNOWN_ISSUE",
                "matched_issue_id": fixed_issue.id,
                "confidence": 0.9,
                "suggested_title": "Fixed renderer bug",
                "root_cause_analysis": "Reappeared",
                "reasoning_summary": "Matched",
            }
        )
    )

    await agent.run_triage(db_session, crash_new, openai_client=FakeOpenAIClient(handler_new))
    await db_session.refresh(crash_new)
    await db_session.refresh(fixed_issue)
    assert crash_new.ai_verdict == "REGRESSION"
    assert fixed_issue.status == "REGRESSION"


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


async def test_dynamic_db_settings_override_and_cross_project_isolation(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    _proj, crash, _ = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path, with_log=False
    )
    other_proj = Project(
        key="foreign-proj",
        name="Foreign",
        api_key_hash=hashlib.sha256(b"k").hexdigest(),
        settings_json={},
    )
    db_session.add(other_proj)
    await db_session.flush()
    foreign_issue = Issue(project_id=other_proj.id, title="Secret foreign issue", status="KNOWN")
    db_session.add(foreign_issue)
    db_session.add(SystemSetting(key="llm_model", value_json={"value": "custom-model-v2"}))
    await db_session.commit()

    def handler(step: int, _k: dict[str, Any]) -> Any:
        if step == 1:
            return _make_response(
                tool_calls=[
                    {
                        "id": "ov",
                        "name": "get_crash_overview",
                        "arguments": {},
                    },
                    {
                        "id": "f1",
                        "name": "get_issue_detail",
                        "arguments": {"issue_id": foreign_issue.id},
                    },
                    {
                        "id": "f2",
                        "name": "get_log_summary",
                        "arguments": {"log_id": str(uuid.uuid4())},
                    },
                ]
            )
        return _make_response(
            content=json.dumps(
                {
                    "verdict": "KNOWN_ISSUE",
                    "matched_issue_id": foreign_issue.id,
                    "confidence": 0.9,
                    "root_cause_analysis": "Attempted match against foreign project issue.",
                }
            )
        )

    client = FakeOpenAIClient(handler)
    agent = AITriageAgent(storage=storage_manager, settings=test_settings)
    run = await agent.run_triage(db_session, crash, openai_client=client)

    # DB llm_model override applied; cross-project issue rejected in both tool and verdict.
    assert run.model_name == "custom-model-v2"
    assert client.calls[0]["model"] == "custom-model-v2"
    tool_msgs = {
        m["tool_call_id"]: json.loads(m["content"])
        for m in client.calls[1]["messages"]
        if m["role"] == "tool"
    }
    assert tool_msgs["f1"] == {"error": "Issue not found"}
    assert tool_msgs["f2"] == {"error": "Log not found"}
    await db_session.refresh(crash)
    assert crash.ai_verdict == "NEEDS_REVIEW"
    assert crash.issue_id is None


async def test_search_known_issues_keyword_ranking_regression_coercion_and_unclustered_new_issue(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    proj, crash, _ = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path, app_version="1.5.0", with_log=False
    )

    # Seed >10 issues with clusters matching the crash stack so stack recall fills top 10,
    # plus 1 unrelated-stack issue whose title matches an explicit keyword query.
    for idx in range(12):
        iss = Issue(
            project_id=proj.id,
            title=f"Renderer stack match #{idx}",
            status="KNOWN",
            component="Renderer",
            crash_count=10 + idx,
        )
        db_session.add(iss)
        await db_session.flush()
        db_session.add(
            CrashCluster(
                project_id=proj.id,
                issue_id=iss.id,
                signature_hash=hashlib.sha256(f"stack-{idx}".encode()).hexdigest(),
                signature_frames_json=[
                    {"module": "renderer.dll", "function": "Renderer::SubmitDraw(int)"}
                ],
                signature_text="renderer.dll!Renderer::SubmitDraw(int)",
                exception_type="EXCEPTION_ACCESS_VIOLATION_READ",
                crash_count=10 + idx,
            )
        )

    keyword_issue = Issue(
        project_id=proj.id,
        title="AudioSocketDeadlock in NetPump",
        status="KNOWN",
        component="Network",
        root_cause="Deadlock on reconnect",
        crash_count=1,
    )
    db_session.add(keyword_issue)
    await db_session.commit()

    # 1. search_known_issues ranks keyword match positively and does not evict it from top 10.
    def kw_handler(step: int, _k: dict[str, Any]) -> Any:
        if step == 1:
            return _make_response(
                tool_calls=[
                    {
                        "id": "kw1",
                        "name": "search_known_issues",
                        "arguments": {"query": "AudioSocketDeadlock"},
                    },
                    {
                        "id": "ov1",
                        "name": "get_crash_overview",
                        "arguments": {},
                    },
                ]
            )
        # 2. LLM returns REGRESSION against a KNOWN (non-FIXED) issue -> coerced to KNOWN_ISSUE.
        return _make_response(
            content=json.dumps(
                {
                    "verdict": "REGRESSION",
                    "matched_issue_id": keyword_issue.id,
                    "confidence": 0.85,
                    "suggested_title": keyword_issue.title,
                    "root_cause_analysis": "Matched known deadlock",
                    "reasoning_summary": "Keyword match",
                }
            )
        )

    client_kw = FakeOpenAIClient(kw_handler)
    agent = AITriageAgent(storage=storage_manager, settings=test_settings)
    run_kw = await agent.run_triage(db_session, crash, openai_client=client_kw)
    assert run_kw.status == "COMPLETED"

    tool_payload = json.loads(
        next(
            m["content"]
            for m in client_kw.calls[1]["messages"]
            if m["role"] == "tool" and m.get("tool_call_id") == "kw1"
        )
    )
    matched_kw_entries = [
        it for it in tool_payload["issues"] if it["issue_id"] == keyword_issue.id
    ]
    assert matched_kw_entries
    assert matched_kw_entries[0]["score"] >= 0.65

    await db_session.refresh(crash)
    await db_session.refresh(keyword_issue)
    assert crash.ai_verdict == "KNOWN_ISSUE"
    assert run_kw.result_json is not None
    assert run_kw.result_json["verdict"] == "KNOWN_ISSUE"
    assert keyword_issue.status == "KNOWN"

    # 3. NEW_ISSUE on unclustered crash (cluster_id=None) with existing issue_id creates a new Issue
    # and decrements the old issue's crash_count.
    unclustered_crash = CrashReport(
        project_id=proj.id,
        cluster_id=None,
        issue_id=keyword_issue.id,
        status="TRIAGING",
        app_version="1.6.0",
        crash_reason="EXCEPTION_ACCESS_VIOLATION_READ",
        parsed_stacks_json=crash.parsed_stacks_json,
        crash_time=datetime.now(UTC) + timedelta(minutes=10),
    )
    keyword_issue.crash_count = 2
    db_session.add(unclustered_crash)
    await db_session.commit()

    old_kw_count = keyword_issue.crash_count
    new_issue_client = FakeOpenAIClient(
        _investigate_then_verdict(
            json.dumps(
                {
                    "verdict": "NEW_ISSUE",
                    "matched_issue_id": None,
                    "confidence": 0.91,
                    "suggested_title": "Brand New Unclustered Crash",
                    "root_cause_analysis": "Distinct failure",
                }
            )
        )
    )
    run_new = await agent.run_triage(db_session, unclustered_crash, openai_client=new_issue_client)
    await db_session.refresh(unclustered_crash)
    await db_session.refresh(keyword_issue)
    assert unclustered_crash.ai_verdict == "NEW_ISSUE"
    assert unclustered_crash.issue_id is not None
    assert unclustered_crash.issue_id != keyword_issue.id
    assert keyword_issue.crash_count == old_kw_count - 1
    assert run_new.result_json is not None
    assert run_new.result_json["matched_issue_id"] == unclustered_crash.issue_id
    created = await db_session.get(Issue, unclustered_crash.issue_id)
    assert created is not None and created.title == "Brand New Unclustered Crash"

    # 4. Reusing cluster.issue_id on NEW_ISSUE updates existing_issue.last_seen_at.
    later_time = datetime.now(UTC) + timedelta(minutes=30)
    reused_cluster = await db_session.get(CrashCluster, crash.cluster_id)
    assert reused_cluster is not None
    reused_cluster.issue_id = keyword_issue.id
    crash_reused = CrashReport(
        project_id=proj.id,
        cluster_id=reused_cluster.id,
        issue_id=None,
        status="TRIAGING",
        app_version="1.7.0",
        crash_reason="EXCEPTION_ACCESS_VIOLATION_READ",
        parsed_stacks_json=crash.parsed_stacks_json,
        crash_time=later_time,
    )
    db_session.add(crash_reused)
    await db_session.commit()
    await agent.run_triage(db_session, crash_reused, openai_client=new_issue_client)
    await db_session.refresh(keyword_issue)
    reused_last_seen = (
        keyword_issue.last_seen_at.replace(tzinfo=UTC)
        if keyword_issue.last_seen_at.tzinfo is None
        else keyword_issue.last_seen_at.astimezone(UTC)
    )
    assert reused_last_seen >= later_time - timedelta(seconds=1)


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


async def test_llm_provider_thinking_level_mapping_and_fallback(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    import httpx
    import openai

    from app.services import llm_providers

    # Verify provider preset builder strategies.
    oa_high = llm_providers.build_openai_completion_kwargs(
        provider_id="openai",
        model_name="o4-mini",
        thinking_level="high",
        messages=[],
        tools=AI_TOOL_DEFINITIONS,
    )
    assert oa_high["reasoning_effort"] == "high"
    assert "temperature" not in oa_high

    qwen_med = llm_providers.build_openai_completion_kwargs(
        provider_id="qwen",
        model_name="qwen3-max",
        thinking_level="medium",
        messages=[],
        tools=AI_TOOL_DEFINITIONS,
    )
    assert qwen_med["extra_body"] == {"enable_thinking": True, "thinking_budget": 8192}

    or_off = llm_providers.build_openai_completion_kwargs(
        provider_id="openrouter",
        model_name="openai/gpt-5",
        thinking_level="off",
        messages=[],
        tools=AI_TOOL_DEFINITIONS,
    )
    assert or_off["extra_body"] == {"reasoning": {"enabled": False}}

    assert (
        llm_providers.normalize_anthropic_base_url("https://api.anthropic.com/v1/messages")
        == "https://api.anthropic.com"
    )

    assert "response_format" not in oa_high
    assert "tools" in oa_high

    # Even if include_response_format=True is passed with tools, response_format must be omitted.
    zhipu_tools = llm_providers.build_openai_completion_kwargs(
        provider_id="zhipu",
        model_name="glm-5.3-flash",
        thinking_level="auto",
        messages=[],
        tools=AI_TOOL_DEFINITIONS,
        include_response_format=True,
    )
    assert "tools" in zhipu_tools
    assert "response_format" not in zhipu_tools

    # Without tools (Phase 2 JSON finalization), response_format is enabled and tools are omitted.
    zhipu_json = llm_providers.build_openai_completion_kwargs(
        provider_id="zhipu",
        model_name="glm-5.3-flash",
        thinking_level="auto",
        messages=[],
        tools=None,
        include_response_format=True,
    )
    assert "tools" not in zhipu_json
    assert "tool_choice" not in zhipu_json
    assert zhipu_json["response_format"] == {"type": "json_object"}

    # Verify multi-step BadRequestError fallback drops thinking params during investigation
    # and drops response_format then thinking params during Phase 2 JSON finalization.
    _project, crash, _log = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path
    )
    captured_kwargs: list[dict[str, Any]] = []

    def _make_bad_request() -> openai.BadRequestError:
        req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        resp = httpx.Response(400, request=req, json={"error": {"message": "unsupported param"}})
        return openai.BadRequestError("unsupported param", response=resp, body=None)

    phase2_started = False

    def _handler(step: int, kwargs: dict[str, Any]) -> Any:
        nonlocal phase2_started
        captured_kwargs.append(dict(kwargs))
        if "response_format" in kwargs:
            phase2_started = True
            raise _make_bad_request()
        if "reasoning_effort" in kwargs:
            raise _make_bad_request()
        if not phase2_started:
            # First investigation turn calls a tool; second returns prose to trigger Phase 2.
            if not any(m.get("role") == "tool" for m in kwargs.get("messages", [])):
                return _make_response(
                    tool_calls=[{"id": "fb1", "name": "get_crash_overview", "arguments": {}}]
                )
            return _make_response(content="Null dereference observed in Renderer::SubmitDraw.")
        return _make_response(
            content=json.dumps(
                {
                    "verdict": "NEW_ISSUE",
                    "matched_issue_id": None,
                    "confidence": 0.88,
                    "suggested_title": "Fallback Model Crash",
                    "root_cause_analysis": "Null dereference in render thread.",
                    "reasoning_summary": "Fallback succeeded.",
                    "evidence_log_snippets": [],
                    "key_log_patterns": [],
                    "suggested_component": "render",
                }
            )
        )

    fake_client = FakeOpenAIClient(_handler)
    agent = AITriageAgent(storage=storage_manager, settings=test_settings)
    run = await agent.run_triage(
        session=db_session,
        crash_report=crash,
        settings_override={
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "llm_thinking_level": "medium",
        },
        openai_client=fake_client,
    )
    assert run.status == "COMPLETED"
    # Investigation turns never send response_format.
    investigation_calls = [k for k in captured_kwargs if "tools" in k]
    assert investigation_calls
    assert all("response_format" not in k for k in investigation_calls)
    # Finalization turns omit tools and fall back: response_format+reasoning -> reasoning -> safe.
    finalization_calls = [k for k in captured_kwargs if "tools" not in k]
    assert len(finalization_calls) == 3
    assert finalization_calls[0]["response_format"] == {"type": "json_object"}
    assert finalization_calls[0]["reasoning_effort"] == "medium"
    assert "response_format" not in finalization_calls[1]
    assert finalization_calls[1]["reasoning_effort"] == "medium"
    assert "response_format" not in finalization_calls[2]
    assert "reasoning_effort" not in finalization_calls[2]


async def test_anthropic_native_protocol_tool_loop_and_thinking_blocks(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    _project, crash, _log = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path
    )
    anthropic_calls: list[dict[str, Any]] = []

    class FakeAnthropicClient:
        def __init__(self) -> None:
            async def _create(**kwargs: Any) -> Any:
                anthropic_calls.append(json.loads(json.dumps(kwargs)))
                if len(anthropic_calls) == 1:
                    return SimpleNamespace(
                        content=[
                            SimpleNamespace(
                                type="thinking",
                                thinking="Let me inspect crash overview first.",
                                signature="sig_turn_1",
                            ),
                            SimpleNamespace(
                                type="tool_use",
                                id="toolu_01",
                                name="get_crash_overview",
                                input={},
                            ),
                        ],
                        usage=SimpleNamespace(input_tokens=120, output_tokens=55),
                    )
                return SimpleNamespace(
                    content=[
                        SimpleNamespace(
                            type="thinking",
                            thinking="Confirmed null dereference in Renderer.",
                            signature="sig_turn_2",
                        ),
                        SimpleNamespace(
                            type="text",
                            text=json.dumps(
                                {
                                    "verdict": "NEW_ISSUE",
                                    "matched_issue_id": None,
                                    "confidence": 0.93,
                                    "suggested_title": "Anthropic Renderer Crash",
                                    "root_cause_analysis": "Access violation at 0x0.",
                                    "reasoning_summary": "Verified via get_crash_overview.",
                                    "evidence_log_snippets": [],
                                    "key_log_patterns": ["FATAL"],
                                    "suggested_component": "renderer",
                                }
                            ),
                        ),
                    ],
                    usage=SimpleNamespace(input_tokens=200, output_tokens=80),
                )

            self.messages = SimpleNamespace(create=_create)

    agent = AITriageAgent(storage=storage_manager, settings=test_settings)
    run = await agent.run_triage(
        session=db_session,
        crash_report=crash,
        settings_override={
            "llm_provider": "anthropic",
            "llm_model": "claude-sonnet-4-5",
            "llm_thinking_level": "low",
        },
        anthropic_client=FakeAnthropicClient(),
    )

    assert run.status == "COMPLETED"
    assert run.model_name == "claude-sonnet-4-5"
    assert run.prompt_tokens == 320
    assert run.completion_tokens == 135
    assert len(anthropic_calls) == 2
    assert anthropic_calls[0]["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert anthropic_calls[0]["max_tokens"] == 6144
    assert "temperature" not in anthropic_calls[0]
    assert anthropic_calls[0]["system"]
    # Turn 2 replays assistant thinking block (with signature) + user tool_result block.
    turn2_msgs = anthropic_calls[1]["messages"]
    assert turn2_msgs[1]["role"] == "assistant"
    assert turn2_msgs[1]["content"][0]["type"] == "thinking"
    assert turn2_msgs[1]["content"][0]["signature"] == "sig_turn_1"
    assert turn2_msgs[2]["role"] == "user"
    assert turn2_msgs[2]["content"][0]["type"] == "tool_result"
    assert turn2_msgs[2]["content"][0]["tool_use_id"] == "toolu_01"
    # Reasoning preview recorded in trace.
    reasoning_steps = [s for s in run.trace_json if s.get("role") == "reasoning"]
    assert len(reasoning_steps) == 2
    assert "crash overview" in reasoning_steps[0]["output_preview"]


async def test_ollama_provider_allows_empty_api_key(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    _project, crash, _log = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path
    )
    created_clients: list[dict[str, Any]] = []

    class DummyAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            created_clients.append(kwargs)
            self._delegate = FakeOpenAIClient(
                _investigate_then_verdict(
                    json.dumps(
                        {
                            "verdict": "NEW_ISSUE",
                            "matched_issue_id": None,
                            "confidence": 0.8,
                            "suggested_title": "Local Ollama Crash",
                            "root_cause_analysis": "Local model analysis.",
                            "reasoning_summary": "Done.",
                            "evidence_log_snippets": [],
                            "key_log_patterns": [],
                            "suggested_component": None,
                        }
                    )
                )
            )
            self.chat = self._delegate.chat

        async def close(self) -> None:
            return None

    monkeypatch.setattr(openai, "AsyncOpenAI", DummyAsyncOpenAI)
    agent = AITriageAgent(storage=storage_manager, settings=test_settings)
    run = await agent.run_triage(
        session=db_session,
        crash_report=crash,
        settings_override={
            "llm_provider": "ollama",
            "llm_base_url": "http://localhost:11434/v1",
            "llm_model": "qwen3:14b",
            "llm_api_key": "",
        },
    )
    assert run.status == "COMPLETED"
    assert len(created_clients) == 1
    assert created_clients[0]["base_url"] == "http://localhost:11434/v1"
    assert created_clients[0]["api_key"] == "not-required"


async def test_two_phase_investigation_without_json_mode_then_structured_finalization(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """Investigation turns omit response_format so tool calling works on GLM-like gateways,
    and prose conclusions transition cleanly to a tools-off JSON-mode finalization turn.
    """
    _proj, crash, log_row = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path, log_lines_count=20
    )
    assert log_row is not None
    seen_kwargs: list[dict[str, Any]] = []

    def handler(step: int, kwargs: dict[str, Any]) -> Any:
        seen_kwargs.append(dict(kwargs))
        if step == 1:
            assert "tools" in kwargs
            assert "response_format" not in kwargs
            return _make_response(
                tool_calls=[{"id": "c1", "name": "get_crash_overview", "arguments": {}}]
            )
        if step == 2:
            assert "tools" in kwargs
            assert "response_format" not in kwargs
            # Model writes natural-language findings after finishing tool investigation.
            return _make_response(
                content=(
                    "After inspecting get_crash_overview, Renderer::SubmitDraw crashed reading "
                    "0x0 due to a null texture pointer."
                )
            )
        # Step 3 must be Phase 2 JSON finalization: tools omitted, response_format=json_object.
        assert "tools" not in kwargs
        assert "tool_choice" not in kwargs
        assert kwargs.get("response_format") == {"type": "json_object"}
        return _make_response(
            content=json.dumps(
                {
                    "verdict": "NEW_ISSUE",
                    "matched_issue_id": None,
                    "confidence": 0.92,
                    "suggested_title": "Null pointer dereference in Renderer::SubmitDraw",
                    "root_cause_analysis": (
                        "Renderer::SubmitDraw dereferences a null texture pointer at 0x0."
                    ),
                    "reasoning_summary": (
                        "Inspected crashing frame via get_crash_overview and confirmed AV at 0x0."
                    ),
                    "evidence_log_snippets": [],
                    "key_log_patterns": ["null texture pointer"],
                    "suggested_component": "Renderer",
                }
            )
        )

    agent = AITriageAgent(storage=storage_manager, settings=test_settings)
    run = await agent.run_triage(
        db_session,
        crash,
        settings_override={"llm_provider": "zhipu", "llm_model": "glm-5.3-flash"},
        openai_client=FakeOpenAIClient(handler),
    )
    assert run.status == "COMPLETED"
    assert len(seen_kwargs) == 3
    assert run.result_json is not None
    assert run.result_json["verdict"] == "NEW_ISSUE"
    assert run.result_json["confidence"] == 0.92
    tool_steps = [s for s in run.trace_json if s.get("role") == "tool"]
    assert len(tool_steps) == 1
    assert tool_steps[0]["tool_name"] == "get_crash_overview"


async def test_empty_or_uninvestigated_needs_review_rejected_and_confidence_not_fabricated(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """Backend must not default omitted confidence to 0.5, must reject empty or uninvestigated
    NEEDS_REVIEW reports as FAILED, and must preserve raw output previews in trace_json.
    """
    # 1. Omitted confidence parses as None rather than fabricated 0.5.
    parsed_no_conf = _parse_verdict_json('{"verdict": "NEEDS_REVIEW"}')
    assert parsed_no_conf["confidence"] is None

    _proj, crash, _log = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path, log_lines_count=20
    )
    agent = AITriageAgent(storage=storage_manager, settings=test_settings)

    # 2. Exact incident reproduction: 0 tool calls + hollow {"verdict": "NEEDS_REVIEW"} ->
    # nudged once, still refuses tools -> FAILED with raw preview in trace and 0.0 confidence.
    hollow_client = FakeOpenAIClient(
        lambda _s, _k: _make_response(content='{"verdict": "NEEDS_REVIEW"}')
    )
    run_hollow = await agent.run_triage(db_session, crash, openai_client=hollow_client)
    assert run_hollow.status == "FAILED"
    assert len(hollow_client.calls) == 2
    assert run_hollow.result_json is not None
    assert run_hollow.result_json["verdict"] == "NEEDS_REVIEW"
    assert run_hollow.result_json["confidence"] == 0.0
    assert any(
        '{"verdict": "NEEDS_REVIEW"}' in str(step.get("output_preview") or "")
        for step in run_hollow.trace_json
    )
    assert any(step.get("error") for step in run_hollow.trace_json)

    # 3. NEEDS_REVIEW with prose excuse but 0 tool calls -> nudged, still 0 tools -> FAILED.
    uninvestigated_client = FakeOpenAIClient(
        lambda _s, _k: _make_response(
            content=json.dumps(
                {
                    "verdict": "NEEDS_REVIEW",
                    "root_cause_analysis": "Insufficient context in prompt.",
                    "reasoning_summary": "Did not inspect crash details.",
                }
            )
        )
    )
    run_uninv = await agent.run_triage(db_session, crash, openai_client=uninvestigated_client)
    assert run_uninv.status == "FAILED"
    assert len(uninvestigated_client.calls) == 2
    assert any(
        "without successfully reading crash evidence" in str(s.get("error") or "")
        for s in run_uninv.trace_json
    )

    # 4. Empty NEEDS_REVIEW even after tool calls -> triggers Phase 2 -> still empty -> FAILED.
    def post_tool_empty_handler(step: int, kwargs: dict[str, Any]) -> Any:
        if step == 1:
            return _make_response(
                tool_calls=[{"id": "e1", "name": "get_crash_overview", "arguments": {}}]
            )
        if step == 3:
            assert "tools" not in kwargs
            assert kwargs.get("response_format") == {"type": "json_object"}
        return _make_response(content='{"verdict": "NEEDS_REVIEW"}')

    run_post_empty = await agent.run_triage(
        db_session, crash, openai_client=FakeOpenAIClient(post_tool_empty_handler)
    )
    assert run_post_empty.status == "FAILED"
    assert any("Empty AI report" in str(s.get("error") or "") for s in run_post_empty.trace_json)

    # 5. Recovery after nudge + legitimate substantive NEEDS_REVIEW with omitted confidence ->
    # COMPLETED with confidence=None (not 0.5).
    def recover_handler(step: int, _k: dict[str, Any]) -> Any:
        if step == 1:
            return _make_response(content='{"verdict": "NEEDS_REVIEW"}')
        if step == 2:
            return _make_response(
                tool_calls=[{"id": "r1", "name": "get_crash_overview", "arguments": {}}]
            )
        return _make_response(
            content=json.dumps(
                {
                    "verdict": "NEEDS_REVIEW",
                    "suggested_title": "Missing symbols in third-party module",
                    "root_cause_analysis": (
                        "Top frames lack symbols and attached logs contain no fatal error."
                    ),
                    "reasoning_summary": (
                        "Called get_crash_overview; unable to determine root cause without PDBs."
                    ),
                }
            )
        )

    run_recovered = await agent.run_triage(
        db_session, crash, openai_client=FakeOpenAIClient(recover_handler)
    )
    assert run_recovered.status == "COMPLETED"
    assert run_recovered.result_json is not None
    assert run_recovered.result_json["verdict"] == "NEEDS_REVIEW"
    assert run_recovered.result_json["confidence"] is None
    await db_session.refresh(crash)
    assert crash.ai_verdict == "NEEDS_REVIEW"
    assert crash.ai_confidence is None


async def test_uninvestigated_new_issue_failed_tool_and_non_string_fields_rejected(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """Enforce P1/P2 guardrails:
    1) Zero-investigation NEW_ISSUE must fail and MUST NOT create an Issue.
    2) Failed tool calls (e.g. Invalid log_id UUID) do not count as completed investigation.
    3) Non-string text fields (e.g. root_cause_analysis=true) must be rejected.
    """
    proj, crash, _log = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path, log_lines_count=20
    )
    agent = AITriageAgent(storage=storage_manager, settings=test_settings)

    # 1. Direct NEW_ISSUE with rich text but 0 tool calls -> nudged once -> still 0 tools ->
    # FAILED and no Issue created in DB.
    zero_tool_new_issue = FakeOpenAIClient(
        lambda _s, _k: _make_response(
            content=json.dumps(
                {
                    "verdict": "NEW_ISSUE",
                    "confidence": 0.91,
                    "suggested_title": "Hallucinated Issue Without Reading Crash",
                    "root_cause_analysis": "Guessed null pointer dereference.",
                    "reasoning_summary": "Guessed from crash_reason only.",
                }
            )
        )
    )
    run_zero = await agent.run_triage(db_session, crash, openai_client=zero_tool_new_issue)
    assert run_zero.status == "FAILED"
    assert len(zero_tool_new_issue.calls) == 2
    await db_session.refresh(crash)
    assert crash.issue_id is None
    assert crash.ai_verdict == "NEEDS_REVIEW"
    issues_after_zero = (
        await db_session.execute(select(Issue).where(Issue.project_id == proj.id))
    ).scalars().all()
    assert issues_after_zero == []

    # 2. Only a failed tool call (Invalid log_id UUID) -> does not count as investigation ->
    # nudged once -> still no successful tool -> FAILED, error preserved in trace, no Issue.
    def failed_tool_handler(step: int, _k: dict[str, Any]) -> Any:
        if step == 1:
            return _make_response(
                tool_calls=[
                    {
                        "id": "bad_log",
                        "name": "get_log_summary",
                        "arguments": {"log_id": "not-a-valid-uuid"},
                    }
                ]
            )
        return _make_response(
            content=json.dumps(
                {
                    "verdict": "NEW_ISSUE",
                    "confidence": 0.85,
                    "suggested_title": "Unverified Issue After Failed Tool",
                    "root_cause_analysis": "Claimed root cause without reading data.",
                    "reasoning_summary": "Ignored tool failure.",
                }
            )
        )

    run_bad_tool = await agent.run_triage(
        db_session, crash, openai_client=FakeOpenAIClient(failed_tool_handler)
    )
    assert run_bad_tool.status == "FAILED"
    assert any(
        step.get("role") == "tool" and "Invalid log_id UUID" in str(step.get("error") or "")
        for step in run_bad_tool.trace_json
    )
    assert any(
        step.get("role") == "system"
        and "without successfully reading crash evidence" in str(step.get("error") or "")
        for step in run_bad_tool.trace_json
    )
    await db_session.refresh(crash)
    assert crash.issue_id is None
    issues_after_bad_tool = (
        await db_session.execute(select(Issue).where(Issue.project_id == proj.id))
    ).scalars().all()
    assert issues_after_bad_tool == []

    # 3. Non-string text fields (bool/int/list) must be rejected by _parse_verdict_json
    # and fail the run even after a successful tool call.
    for bad_payload in (
        {"verdict": "NEW_ISSUE", "root_cause_analysis": True},
        {"verdict": "NEW_ISSUE", "reasoning_summary": 123},
        {"verdict": "NEW_ISSUE", "suggested_title": ["bad"], "root_cause_analysis": "ok"},
        {"verdict": "NEW_ISSUE", "root_cause_analysis": "ok", "suggested_component": True},
    ):
        with pytest.raises(ValueError, match="must be a string"):
            _parse_verdict_json(json.dumps(bad_payload))

    non_str_client = FakeOpenAIClient(
        _investigate_then_verdict(
            json.dumps(
                {
                    "verdict": "NEW_ISSUE",
                    "confidence": 0.8,
                    "suggested_title": "Bool Root Cause",
                    "root_cause_analysis": True,
                }
            )
        )
    )
    run_non_str = await agent.run_triage(db_session, crash, openai_client=non_str_client)
    assert run_non_str.status == "FAILED"
    assert any(
        "root_cause_analysis must be a string" in str(step.get("error") or "")
        for step in run_non_str.trace_json
    )
    await db_session.refresh(crash)
    assert crash.issue_id is None


async def test_kb_only_tools_rejected_and_max_steps_one_completes_with_summary_turn(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """Enforce current-crash evidence requirement and AI_MAX_STEPS=1 finalization:
    1) Calling only search_known_issues or get_issue_detail must NOT allow auto-creating or
       linking an Issue.
    2) With ai_max_steps=1, after reading get_crash_overview, one tools-disabled JSON summary
       turn allows the run to complete cleanly.
    """
    proj, crash, _log = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path, log_lines_count=20
    )
    unrelated_issue = Issue(
        project_id=proj.id,
        title="Unrelated Audio Bug",
        status="KNOWN",
        component="Audio",
        crash_count=1,
    )
    db_session.add(unrelated_issue)
    await db_session.commit()

    agent = AITriageAgent(storage=storage_manager, settings=test_settings)

    # 1a. Only search_known_issues (returns empty) -> cannot auto-create NEW_ISSUE.
    def kb_search_only_handler(step: int, _k: dict[str, Any]) -> Any:
        if step == 1:
            return _make_response(
                tool_calls=[
                    {
                        "id": "kb1",
                        "name": "search_known_issues",
                        "arguments": {"query": "NonExistentQuery"},
                    }
                ]
            )
        return _make_response(
            content=json.dumps(
                {
                    "verdict": "NEW_ISSUE",
                    "confidence": 0.88,
                    "suggested_title": "Guess Without Reading Crash",
                    "root_cause_analysis": "No known issue matched search.",
                    "reasoning_summary": "Searched issues only.",
                }
            )
        )

    run_kb_search = await agent.run_triage(
        db_session, crash, openai_client=FakeOpenAIClient(kb_search_only_handler)
    )
    assert run_kb_search.status == "FAILED"
    await db_session.refresh(crash)
    assert crash.issue_id is None
    project_issues = (
        await db_session.execute(select(Issue).where(Issue.project_id == proj.id))
    ).scalars().all()
    assert [iss.id for iss in project_issues] == [unrelated_issue.id]

    # 1b. Only get_issue_detail on an unrelated issue -> cannot link KNOWN_ISSUE.
    def kb_detail_only_handler(step: int, _k: dict[str, Any]) -> Any:
        if step == 1:
            return _make_response(
                tool_calls=[
                    {
                        "id": "kb2",
                        "name": "get_issue_detail",
                        "arguments": {"issue_id": unrelated_issue.id},
                    }
                ]
            )
        return _make_response(
            content=json.dumps(
                {
                    "verdict": "KNOWN_ISSUE",
                    "matched_issue_id": unrelated_issue.id,
                    "confidence": 0.9,
                    "suggested_title": unrelated_issue.title,
                    "root_cause_analysis": "Linked without inspecting current crash.",
                    "reasoning_summary": "Read unrelated issue detail only.",
                }
            )
        )

    run_kb_detail = await agent.run_triage(
        db_session, crash, openai_client=FakeOpenAIClient(kb_detail_only_handler)
    )
    assert run_kb_detail.status == "FAILED"
    await db_session.refresh(crash)
    await db_session.refresh(unrelated_issue)
    assert crash.issue_id is None
    assert unrelated_issue.crash_count == 1

    # 2. ai_max_steps=1: step 1 reads get_crash_overview -> post-budget tools-disabled JSON turn
    # completes the report cleanly.
    seen_kwargs: list[dict[str, Any]] = []

    def max_steps_one_handler(step: int, kwargs: dict[str, Any]) -> Any:
        seen_kwargs.append(dict(kwargs))
        if step == 1:
            assert "tools" in kwargs
            assert "response_format" not in kwargs
            return _make_response(
                tool_calls=[{"id": "ov_one", "name": "get_crash_overview", "arguments": {}}]
            )
        assert "tools" not in kwargs
        assert "tool_choice" not in kwargs
        assert kwargs.get("response_format") == {"type": "json_object"}
        return _make_response(
            content=json.dumps(
                {
                    "verdict": "NEW_ISSUE",
                    "matched_issue_id": None,
                    "confidence": 0.91,
                    "suggested_title": "Single-Step Overview Crash",
                    "root_cause_analysis": "Null deref in Renderer::SubmitDraw from overview.",
                    "reasoning_summary": "Inspected crash overview within 1 tool step.",
                    "evidence_log_snippets": [],
                    "key_log_patterns": [],
                    "suggested_component": "Renderer",
                }
            )
        )

    run_one = await agent.run_triage(
        db_session,
        crash,
        settings_override={"ai_max_steps": 1},
        openai_client=FakeOpenAIClient(max_steps_one_handler),
    )
    assert run_one.status == "COMPLETED"
    assert len(seen_kwargs) == 2
    await db_session.refresh(crash)
    assert crash.ai_verdict == "NEW_ISSUE"
    assert crash.issue_id is not None


async def test_empty_or_truncated_evidence_rejected_and_post_budget_summary_error_preserved(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """Enforce delivered-content evidence checks and preserve post-budget summary errors:
    1) Out-of-range read_log_lines returning empty lines=[] does not count as crash evidence.
    2) get_crash_overview truncated to 100 chars (IDs only, no stack frames) does not count.
    3) Post-budget summary returning non-string root_cause_analysis preserves the exact parse
       error in trace_json and result_json instead of generic Exceeded AI_MAX_STEPS.
    """
    proj, crash, log_row = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path, log_lines_count=20
    )
    assert log_row is not None
    agent = AITriageAgent(storage=storage_manager, settings=test_settings)

    # 1. Out-of-range read_log_lines returns lines=[] -> cannot auto-create NEW_ISSUE.
    def empty_log_range_handler(step: int, _k: dict[str, Any]) -> Any:
        if step == 1:
            return _make_response(
                tool_calls=[
                    {
                        "id": "oor1",
                        "name": "read_log_lines",
                        "arguments": {
                            "log_id": str(log_row.id),
                            "start_line": 9999,
                            "end_line": 10020,
                        },
                    }
                ]
            )
        return _make_response(
            content=json.dumps(
                {
                    "verdict": "NEW_ISSUE",
                    "confidence": 0.9,
                    "suggested_title": "Guess From Empty Log Range",
                    "root_cause_analysis": "Guessed root cause without log content.",
                    "reasoning_summary": "Read out-of-range lines only.",
                }
            )
        )

    run_oor = await agent.run_triage(
        db_session, crash, openai_client=FakeOpenAIClient(empty_log_range_handler)
    )
    assert run_oor.status == "FAILED"
    await db_session.refresh(crash)
    assert crash.issue_id is None
    assert (
        await db_session.execute(select(Issue).where(Issue.project_id == proj.id))
    ).scalars().all() == []

    # 2. get_crash_overview truncated to 100 chars (IDs only, no top_frames) -> cannot create Issue.
    def truncated_overview_handler(step: int, _k: dict[str, Any]) -> Any:
        if step == 1:
            return _make_response(
                tool_calls=[{"id": "trunc_ov", "name": "get_crash_overview", "arguments": {}}]
            )
        return _make_response(
            content=json.dumps(
                {
                    "verdict": "NEW_ISSUE",
                    "confidence": 0.85,
                    "suggested_title": "Guess From Truncated Overview",
                    "root_cause_analysis": "Never saw stack frames due to 100-char truncation.",
                    "reasoning_summary": "Overview was cut off at IDs.",
                }
            )
        )

    run_trunc = await agent.run_triage(
        db_session,
        crash,
        settings_override={"ai_tool_output_max_chars": 100},
        openai_client=FakeOpenAIClient(truncated_overview_handler),
    )
    assert run_trunc.status == "FAILED"
    await db_session.refresh(crash)
    assert crash.issue_id is None
    assert (
        await db_session.execute(select(Issue).where(Issue.project_id == proj.id))
    ).scalars().all() == []

    # 3. ai_max_steps=1: step 1 gets real overview, post-budget summary returns boolean
    # root_cause_analysis -> preserves exact parse error (not generic Exceeded AI_MAX_STEPS).
    def bad_summary_after_budget_handler(step: int, _k: dict[str, Any]) -> Any:
        if step == 1:
            return _make_response(
                tool_calls=[{"id": "ov_ok", "name": "get_crash_overview", "arguments": {}}]
            )
        return _make_response(
            content=json.dumps(
                {
                    "verdict": "NEW_ISSUE",
                    "confidence": 0.9,
                    "suggested_title": "Bad Post-Budget Summary",
                    "root_cause_analysis": True,
                }
            )
        )

    run_bad_summary = await agent.run_triage(
        db_session,
        crash,
        settings_override={"ai_max_steps": 1},
        openai_client=FakeOpenAIClient(bad_summary_after_budget_handler),
    )
    assert run_bad_summary.status == "FAILED"
    assert run_bad_summary.result_json is not None
    assert (
        "root_cause_analysis must be a string"
        in run_bad_summary.result_json["root_cause_analysis"]
    )
    assert any(
        step.get("role") == "system"
        and "root_cause_analysis must be a string" in str(step.get("error") or "")
        for step in run_bad_summary.trace_json
    )
    assert not any(
        "Exceeded AI_MAX_STEPS" in str(step.get("error") or "")
        for step in run_bad_summary.trace_json
    )
    await db_session.refresh(crash)
    assert crash.issue_id is None


def test_trim_never_shortens_instruction_address() -> None:
    """P1 regression: an address that does not fit whole is omitted, never clipped."""
    ip = "0x00007ff700001234"
    frame = {"frame_index": 0, "ip": ip, "module": None, "function": None}
    payload: dict[str, Any] = {"top_frames": [frame], "missing_symbols": []}
    skeleton_len = len(json.dumps({**payload, "top_frames": []}))
    # Room for a few short fields, but not for the 20-char address.
    max_chars = skeleton_len + len("\n...[TRUNCATED]") + 45

    _, trimmed = _prepare_tool_output_for_llm(
        "get_crash_overview", json.dumps(payload), max_chars
    )

    assert trimmed is not None
    frames = trimmed["top_frames"]
    assert all(f.get("ip") in (None, ip) for f in frames)
    # Without a whole identifier the frame is no longer crash evidence.
    assert frames == []


def test_trim_does_not_treat_a_function_preview_as_full_stack_evidence() -> None:
    function = "R::flushPending<" + "X" * 3000 + ">"
    payload = {"top_frames": [{"frame_index": 0, "function": function}], "missing_symbols": []}
    preview_payload = {**payload, "top_frames": [{"frame_index": 0, "function_preview": "R::f"}]}
    max_chars = len(json.dumps(preview_payload)) + len("\n...[TRUNCATED]")

    delivered, trimmed = _prepare_tool_output_for_llm(
        "get_crash_overview", json.dumps(payload), max_chars
    )

    assert trimmed is not None
    assert trimmed["top_frames"] == []
    assert json.loads(delivered.removesuffix("\n...[TRUNCATED]")) == trimmed


def test_trim_keeps_log_body_before_timestamp_and_level() -> None:
    """P2 regression: visible log text takes budget before timestamp/level metadata."""
    entry = {
        "line_number": 7,
        "text": "FATAL: " + "x" * 3000,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "level": "FATAL",
    }
    payload: dict[str, Any] = {"log_id": "abc", "lines": [entry]}
    skeleton_len = len(json.dumps({**payload, "lines": []}))
    # Room for line_number and ~10 chars of text, but not timestamp plus level as well.
    max_chars = skeleton_len + len("\n...[TRUNCATED]") + 40

    _, trimmed = _prepare_tool_output_for_llm(
        "read_log_lines", json.dumps(payload), max_chars
    )

    assert trimmed is not None
    assert len(trimmed["lines"]) == 1
    kept = trimmed["lines"][0]
    assert kept["line_number"] == 7
    assert kept["text"].startswith("FATAL:")


def test_trim_keeps_next_log_entry_after_short_entry() -> None:
    """P2 regression: a short INFO then a long FATAL must keep the FATAL (clipped).

    Before the fix, _fit_partial_entry was only called when fitted == 0.  With a short
    INFO already placed, fitted == 1 and the long FATAL was silently discarded.
    """
    short_entry = {"line_number": 1, "text": "INFO start", "timestamp": None, "level": "INFO"}
    long_text = "FATAL: " + "x" * 3000
    long_entry = {"line_number": 2, "text": long_text, "timestamp": None, "level": "FATAL"}
    payload = {
        "log_id": "abc",
        "total_lines": 2,
        "start_line": 1,
        "end_line": 2,
        "capped_to_200": False,
        "lines": [short_entry, long_entry],
    }
    raw = json.dumps(payload)
    # Budget: fits short_entry but not long_entry in full; leaves ~200 chars spare for a clip.
    short_only_len = len(json.dumps({**payload, "lines": [short_entry]}))
    max_chars = short_only_len + 200

    delivered, trimmed = _prepare_tool_output_for_llm("read_log_lines", raw, max_chars)

    assert trimmed is not None
    lines = trimmed["lines"]
    assert len(lines) == 2, "both entries must be present in trimmed output"
    assert lines[0]["text"] == "INFO start"
    assert lines[1]["text"].startswith("FATAL:"), "FATAL text must be partially retained"
    assert len(lines[1]["text"]) < len(long_text), "long text must have been clipped"
    assert delivered.endswith("...[TRUNCATED]")


def test_trim_keeps_frame_short_fields_when_function_too_long() -> None:
    """P2 regression: a frame whose function name is too long must still deliver ip and module.

    Before the fix, top_frames had no partial-entry path, so a frame that did not fit as
    a whole was dropped entirely, losing the instruction address and module name.
    """
    long_func = "LongNamespace::" + "A" * 4000 + "::method(int, int)"
    frame = {
        "frame_index": 0,
        "ip": "0x00007ff700001234",
        "module": "engine.dll",
        "function": long_func,
        "file": None,
    }
    payload: dict[str, Any] = {
        "top_frames": [frame],
        "missing_symbols": [],
        "logs": [],
        "candidate_issues": [],
    }
    raw = json.dumps(payload)
    # Budget: fits the empty-list skeleton plus ~200 chars — enough for ip/module but not
    # the multi-kilobyte function name.
    skeleton_len = len(json.dumps({**payload, "top_frames": []}))
    max_chars = skeleton_len + 200

    delivered, trimmed = _prepare_tool_output_for_llm("get_crash_overview", raw, max_chars)

    assert trimmed is not None
    frames = trimmed["top_frames"]
    assert len(frames) == 1, "partial frame must be retained"
    assert frames[0].get("ip") == "0x00007ff700001234", "instruction address must be preserved"
    assert frames[0].get("module") == "engine.dll", "module name must be preserved"
    assert "function" not in frames[0]
    preview = frames[0]["function_preview"]
    assert 0 < len(preview) < len(long_func)
    assert long_func.startswith(preview)
    assert len(delivered) <= max_chars
    assert delivered.endswith("...[TRUNCATED]")


async def test_structured_evidence_blank_logs_second_thread_and_ip_only_frames(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """Verify exact structured evidence validation for all three edge cases:
    1) Default 8000-char limit with 130 blank log lines followed by '5' (truncated out):
       line numbers must never spoof log text -> FAILED and no Issue created.
    2) get_other_threads where first non-crashing thread has frames=[] and second thread
       has visible frames -> counts as valid crash evidence and completes.
    3) Unsymbolicated top_frames containing only instruction address `ip` -> counts as
       valid crash evidence and supports a COMPLETED NEEDS_REVIEW report.
    """
    proj, crash, _ = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path, with_log=False
    )

    # 1. Log with 150 blank lines followed by a single non-empty line "5" that gets trimmed out
    # under the default 8000-char tool output limit.
    blank_log_id = uuid.uuid4()
    raw_blank_log = tmp_path / f"blank_{blank_log_id.hex}.log"
    raw_blank_log.write_bytes((b"\n" * 150) + b"5\n")
    rel_zst = f"logs/{crash.id}/{blank_log_id}.log.zst"
    rel_idx = f"logs/{crash.id}/{blank_log_id}.idx.json"
    abs_zst = storage_manager.resolve_path(rel_zst)
    abs_idx = storage_manager.resolve_path(rel_idx)
    abs_zst.parent.mkdir(parents=True, exist_ok=True)
    ingest_info = LogEngine().ingest_log(raw_blank_log, abs_zst, abs_idx)
    blank_log = CrashLog(
        id=blank_log_id,
        crash_id=crash.id,
        filename="blank.log",
        zst_path=rel_zst,
        idx_path=rel_idx,
        raw_size=ingest_info["raw_size"],
        compressed_size=ingest_info["compressed_size"],
        total_lines=ingest_info["total_lines"],
        digest_json=ingest_info["digest_json"],
    )
    db_session.add(blank_log)
    await db_session.commit()

    agent = AITriageAgent(storage=storage_manager, settings=test_settings)

    def blank_lines_handler(step: int, _k: dict[str, Any]) -> Any:
        if step == 1:
            return _make_response(
                tool_calls=[
                    {
                        "id": "bl1",
                        "name": "read_log_lines",
                        "arguments": {
                            "log_id": str(blank_log.id),
                            "start_line": 1,
                            "end_line": 151,
                        },
                    }
                ]
            )
        return _make_response(
            content=json.dumps(
                {
                    "verdict": "NEW_ISSUE",
                    "confidence": 0.9,
                    "suggested_title": "Hallucinated From Blank Log Lines",
                    "root_cause_analysis": "Guessed despite only receiving blank log lines.",
                    "reasoning_summary": "Line 151 was truncated out.",
                }
            )
        )

    blank_client = FakeOpenAIClient(blank_lines_handler)
    run_blank = await agent.run_triage(
        db_session,
        crash,
        settings_override={"ai_max_steps": 20, "ai_tool_output_max_chars": 8000},
        openai_client=blank_client,
    )
    assert run_blank.status == "FAILED"
    tool_msg_content = next(
        m["content"] for m in blank_client.calls[1]["messages"] if m["role"] == "tool"
    )
    assert tool_msg_content.endswith("...[TRUNCATED]")
    delivered_json = json.loads(tool_msg_content[: -len("\n...[TRUNCATED]")])
    assert len(delivered_json["lines"]) > 0
    assert all(item["text"] == "" for item in delivered_json["lines"])
    await db_session.refresh(crash)
    assert crash.issue_id is None
    assert (
        await db_session.execute(select(Issue).where(Issue.project_id == proj.id))
    ).scalars().all() == []

    # 2. First non-crashing thread has empty frames=[], second has real frames -> valid evidence.
    crash.parsed_stacks_json = {
        "crashing_thread_index": 0,
        "threads": [
            {"thread_index": 0, "thread_id": 100, "thread_name": "Main", "frames": []},
            {"thread_index": 1, "thread_id": 101, "thread_name": "EmptyWorker", "frames": []},
            {
                "thread_index": 2,
                "thread_id": 102,
                "thread_name": "RealWorker",
                "frames": [
                    {
                        "frame_index": 0,
                        "module": "IO.dll",
                        "function": "DiskCache::Flush()",
                        "ip": "0x00007ff700001234",
                    }
                ],
            },
        ],
    }
    await db_session.commit()

    def other_threads_handler(step: int, _k: dict[str, Any]) -> Any:
        if step == 1:
            return _make_response(
                tool_calls=[{"id": "ot1", "name": "get_other_threads", "arguments": {}}]
            )
        return _make_response(
            content=json.dumps(
                {
                    "verdict": "NEW_ISSUE",
                    "confidence": 0.82,
                    "suggested_title": "DiskCache Flush Hang",
                    "root_cause_analysis": "RealWorker thread stuck in IO.dll!DiskCache::Flush().",
                    "reasoning_summary": "Inspected non-crashing threads via get_other_threads.",
                }
            )
        )

    run_threads = await agent.run_triage(
        db_session, crash, openai_client=FakeOpenAIClient(other_threads_handler)
    )
    assert run_threads.status == "COMPLETED"
    await db_session.refresh(crash)
    assert crash.ai_verdict == "NEW_ISSUE"
    assert crash.issue_id is not None

    # 3. Unsymbolicated crashing frame with only instruction address `ip` supports NEEDS_REVIEW.
    ip_only_crash = CrashReport(
        project_id=proj.id,
        status="TRIAGING",
        app_version="1.0.0",
        crash_reason="EXCEPTION_ACCESS_VIOLATION_READ",
        parsed_stacks_json={
            "crashing_thread_index": 0,
            "threads": [
                {
                    "thread_index": 0,
                    "thread_id": 300,
                    "thread_name": "Main",
                    "frames": [
                        {
                            "frame_index": 0,
                            "ip": "0x00007ff712345678",
                            "module": None,
                            "function": None,
                            "missing_symbols": True,
                        }
                    ],
                }
            ],
        },
        missing_symbols_json=[],
    )
    db_session.add(ip_only_crash)
    await db_session.commit()

    ip_only_client = FakeOpenAIClient(
        _investigate_then_verdict(
            json.dumps(
                {
                    "verdict": "NEEDS_REVIEW",
                    "confidence": 0.25,
                    "suggested_title": "Unsymbolicated crash at 0x00007ff712345678",
                    "root_cause_analysis": (
                        "Top frame only exposes raw instruction address 0x00007ff712345678 "
                        "without loaded module or PDB symbols."
                    ),
                    "reasoning_summary": (
                        "Called get_crash_overview and confirmed only raw IP is available."
                    ),
                }
            )
        )
    )
    run_ip_only = await agent.run_triage(db_session, ip_only_crash, openai_client=ip_only_client)
    assert run_ip_only.status == "COMPLETED"
    await db_session.refresh(ip_only_crash)
    assert ip_only_crash.ai_verdict == "NEEDS_REVIEW"
    assert ip_only_crash.ai_confidence == 0.25


async def test_ai_agent_session_midfile_restart_tools_fallback_and_trim_priority(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """AI agent gets mid-file session context, related crashes, fallback, and trim priority."""
    proj = Project(
        key=f"sess-ai-{uuid.uuid4().hex[:6]}",
        name="Session AI Project",
        api_key_hash=hashlib.sha256(b"key").hexdigest(),
        settings_json={"session_id_label": "FlowID"},
    )
    db_session.add(proj)
    await db_session.flush()

    # Delayed manual upload: crash_time is 4 hours AFTER the actual crashing session in the log
    delayed_upload_time = datetime(2026, 11, 24, 14, 0, 0, tzinfo=UTC)
    stacks = {
        "crashing_thread_index": 0,
        "threads": [
            {
                "thread_index": 0,
                "thread_id": 100,
                "thread_name": "MainThread",
                "frames": [
                    {
                        "frame_index": 0,
                        "module": "Renderer.dll",
                        "function": "Renderer::Present()",
                        "module_offset": "0x200",
                        "file": "present.cpp",
                        "line": 88,
                        "missing_symbols": False,
                        "inlines": [],
                    }
                ],
            }
        ],
        "modules": [],
    }
    crash = CrashReport(
        project_id=proj.id,
        status="TRIAGING",
        app_version="2.1.0",
        session_id="flow-crash-777",
        crash_reason="EXCEPTION_ACCESS_VIOLATION_READ",
        crash_address="0x0",
        parsed_summary_json={"top_frames": stacks["threads"][0]["frames"]},
        parsed_stacks_json=stacks,
        missing_symbols_json=[],
        crash_time=delayed_upload_time,
    )
    sibling = CrashReport(
        project_id=proj.id,
        status="ANALYZED",
        app_version="2.1.0",
        session_id="flow-crash-777",
        crash_reason="EXCEPTION_ILLEGAL_INSTRUCTION",
        crash_address="0x40",
        crash_time=delayed_upload_time - timedelta(minutes=1),
    )
    db_session.add_all([crash, sibling])
    await db_session.flush()

    log_id = uuid.uuid4()
    raw_log = tmp_path / "midfile_ai.log"
    raw_log.write_text(
        "\n".join(
            [
                "2026-11-24 09:00:01.000 INFO [old-flow-001] old boot",
                "2026-11-24 10:00:01.000 INFO [flow-crash-777] streaming start",
                "2026-11-24 10:00:08.000 ERROR [flow-crash-777] DXGI_ERROR_DEVICE_REMOVED",
                "2026-11-24 10:00:08.200 FATAL Unhandled exception 0xC0000005 writing minidump",
                "2026-11-24 10:00:12.000 INFO === Process auto-restarted ===",
                "2026-11-24 13:55:00.000 ERROR [restarted-flow-888] unrelated network timeout",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    rel_zst = f"logs/{crash.id}/{log_id}.log.zst"
    rel_idx = f"logs/{crash.id}/{log_id}.idx.json"
    abs_zst = storage_manager.resolve_path(rel_zst)
    abs_idx = storage_manager.resolve_path(rel_idx)
    abs_zst.parent.mkdir(parents=True, exist_ok=True)
    ingest_res = LogEngine().ingest_log(
        raw_log,
        abs_zst,
        abs_idx,
        session_id=crash.session_id,
    )
    db_session.add(
        CrashLog(
            id=log_id,
            crash_id=crash.id,
            filename="runtime.log",
            zst_path=rel_zst,
            idx_path=rel_idx,
            raw_size=ingest_res["raw_size"],
            compressed_size=ingest_res["compressed_size"],
            total_lines=ingest_res["total_lines"],
            digest_json=ingest_res["digest_json"],
        )
    )
    await db_session.commit()

    observed_tools: dict[str, dict[str, Any]] = {}

    def handler(step: int, kwargs: dict[str, Any]) -> Any:
        messages = kwargs["messages"]
        if step == 1:
            assert "session_id [FlowID]=flow-crash-777" in messages[1]["content"]
            return _make_response(
                tool_calls=[{"id": "c1", "name": "get_crash_overview", "arguments": {}}]
            )
        if step == 2:
            observed_tools["overview"] = json.loads(messages[-1]["content"])
            return _make_response(
                tool_calls=[
                    {
                        "id": "c2",
                        "name": "get_log_summary",
                        "arguments": {"log_id": str(log_id)},
                    }
                ]
            )
        if step == 3:
            observed_tools["summary"] = json.loads(messages[-1]["content"])
            return _make_response(
                tool_calls=[
                    {
                        "id": "c3",
                        "name": "read_log_around_time",
                        "arguments": {"log_id": str(log_id), "only_session": True},
                    }
                ]
            )
        if step == 4:
            observed_tools["around"] = json.loads(messages[-1]["content"])
            return _make_response(
                tool_calls=[
                    {
                        "id": "c4",
                        "name": "search_log",
                        "arguments": {
                            "log_id": str(log_id),
                            "pattern": "ERROR",
                            "only_session": True,
                        },
                    }
                ]
            )
        if step == 5:
            observed_tools["search"] = json.loads(messages[-1]["content"])
            return _make_response(
                content=json.dumps(
                    {
                        "verdict": "NEW_ISSUE",
                        "matched_issue_id": None,
                        "confidence": 0.93,
                        "suggested_title": "DXGI device removed in Renderer::Present",
                        "root_cause_analysis": (
                            "Mid-file session flow-crash-777 hit DXGI_ERROR_DEVICE_REMOVED at L3 "
                            "followed by untagged fatal crash at L4 before process restart."
                        ),
                        "reasoning_summary": (
                            "Isolated flow-crash-777 from post-restart noise via session tools."
                        ),
                        "evidence_log_snippets": [
                            {
                                "log_id": str(log_id),
                                "start_line": 2,
                                "end_line": 4,
                                "relevance": "Session error and untagged crash boundary",
                            }
                        ],
                        "key_log_patterns": ["DXGI_ERROR_DEVICE_REMOVED"],
                        "suggested_component": "Rendering",
                    }
                )
            )
        raise AssertionError(f"Unexpected step {step}")

    agent = AITriageAgent(storage=storage_manager, settings=test_settings)
    run = await agent.run_triage(db_session, crash, openai_client=FakeOpenAIClient(handler))
    assert run.status == "COMPLETED"

    ov = observed_tools["overview"]
    assert ov["session_id"] == "flow-crash-777"
    assert ov["session_id_label"] == "FlowID"
    assert ov["related_session_crash_count"] == 1
    assert ov["related_session_crashes"][0]["crash_id"] == str(sibling.id)
    assert ov["logs"][0]["session_first_line"] == 2
    assert ov["logs"][0]["session_last_line"] == 3
    assert ov["logs"][0]["trailing_lines_after_session"] == 3

    sm = observed_tools["summary"]
    assert sm["session_id"] == "flow-crash-777"
    assert [e["line_number"] for e in sm["session_recent_errors"]] == [3]
    assert len(sm["post_session_lines"]) == 3
    assert "Unhandled exception" in sm["post_session_lines"][0]["text"]

    # Automatic fallback from delayed upload crash_time to session_last_ts
    around = observed_tools["around"]
    assert around["session_id"] == "flow-crash-777"
    assert around["anchored_to"] == "session_last_ts"
    assert [ln["line_number"] for ln in around["lines"]] == [2, 3]

    srch = observed_tools["search"]
    assert srch["session_id"] == "flow-crash-777"
    assert [m["line_number"] for m in srch["matches"]] == [3]

    # Tool output trimmer prioritizes session errors/tail/post-session over global noise
    noisy_summary = {
        "log_id": str(log_id),
        "filename": "runtime.log",
        "session_id": "flow-crash-777",
        "session_recent_errors": [
            {"line_number": 3, "text": "DXGI_ERROR_DEVICE_REMOVED [flow-crash-777]"}
        ],
        "session_tail_lines": [
            {"line_number": 2, "text": "streaming start [flow-crash-777]"}
        ],
        "post_session_lines": [
            {"line_number": 4, "text": "FATAL Unhandled exception writing minidump"}
        ],
        "recent_errors": [
            {"line_number": 100 + i, "text": f"unrelated post-restart error {i} " + ("N" * 80)}
            for i in range(20)
        ],
        "tail_lines": [
            {"line_number": 200 + i, "text": f"unrelated post-restart tail {i} " + ("T" * 80)}
            for i in range(20)
        ],
    }
    _text, trimmed = _prepare_tool_output_for_llm(
        "get_log_summary", json.dumps(noisy_summary), max_chars=700
    )
    assert trimmed is not None
    assert len(trimmed["session_recent_errors"]) == 1
    assert len(trimmed["session_tail_lines"]) == 1
    assert len(trimmed["post_session_lines"]) == 1
    assert len(trimmed["recent_errors"]) < 20

