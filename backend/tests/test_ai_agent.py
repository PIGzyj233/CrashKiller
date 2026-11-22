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

    def handler(_step: int, _kwargs: dict[str, Any]) -> Any:
        return _make_response(
            content=json.dumps(
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
    def handler_old(_step: int, _kwargs: dict[str, Any]) -> Any:
        return _make_response(
            content=json.dumps(
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

    def handler_new(_step: int, _kwargs: dict[str, Any]) -> Any:
        return _make_response(
            content=json.dumps(
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
                    }
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
        next(m["content"] for m in client_kw.calls[1]["messages"] if m["role"] == "tool")
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
        lambda _s, _k: _make_response(
            content=json.dumps(
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
        lambda _s, _k: _make_response(
            content='{"verdict": "NEW_ISSUE", "confidence": NaN, "suggested_title": "NaN Conf"}'
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
            lambda _s, _k: _make_response(
                content=json.dumps(
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
