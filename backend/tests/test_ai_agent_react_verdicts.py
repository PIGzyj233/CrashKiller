"""Unit tests for AITriageAgent ReAct loop, tool execution, verdicts, and prompts."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    CrashCluster,
    CrashReport,
    Issue,
    IssueStatusEvent,
    Project,
    SystemSetting,
)
from app.services.ai_agent import (
    AI_TOOL_DEFINITIONS,
    AITriageAgent,
    run_crash_triage,
)
from app.services.storage import StorageManager
from tests._ai_agent_helpers import (
    FakeOpenAIClient,
    _investigate_then_verdict,
    _make_response,
    _seed_crash_with_log,
)


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

    reg_events = (
        await db_session.execute(
            select(IssueStatusEvent).where(IssueStatusEvent.issue_id == fixed_issue.id)
        )
    ).scalars().all()
    assert len(reg_events) == 1
    assert reg_events[0].from_status == "FIXED"
    assert reg_events[0].to_status == "REGRESSION"
    assert reg_events[0].source == "ai_regression"
    assert reg_events[0].fixed_in_version == "1.2.0"
    assert reg_events[0].crash_id == crash_new.id


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


async def test_ai_triage_prompt_respects_output_language_and_markdown_rules(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    _proj, crash, _log = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path, with_log=False
    )

    zh_verdict = json.dumps(
        {
            "verdict": "NEW_ISSUE",
            "matched_issue_id": None,
            "confidence": 0.86,
            "suggested_title": "渲染提交阶段访问空纹理指针导致崩溃 (Renderer::SubmitDraw)",
            "root_cause_analysis": (
                "### 核心根因\n\n`Renderer::SubmitDraw` 在解引用纹理指针前缺少判空。\n\n"
                "### 修复与排查建议\n- 在 `renderer.cpp:128` 处补充空指针校验。"
            ),
            "reasoning_summary": (
                "1. **堆栈分析**：崩溃位于 `Renderer.dll!Renderer::SubmitDraw`。\n\n"
                "**最终结论**：判定为新问题。"
            ),
            "evidence_log_snippets": [],
            "key_log_patterns": ["Renderer::SubmitDraw"],
            "suggested_component": "Renderer",
        },
        ensure_ascii=False,
    )

    # Default language is zh-CN -> system & user prompts must require Simplified Chinese + Markdown.
    zh_client = FakeOpenAIClient(_investigate_then_verdict(zh_verdict))
    agent = AITriageAgent(storage=storage_manager, settings=test_settings)
    zh_run = await agent.run_triage(db_session, crash, openai_client=zh_client)
    assert zh_run.status == "COMPLETED"
    zh_sys_prompt = zh_client.calls[0]["messages"][0]["content"]
    zh_user_prompt = zh_client.calls[0]["messages"][1]["content"]
    assert "简体中文" in zh_sys_prompt
    assert "Markdown" in zh_sys_prompt
    assert "### 根因与触发原因" in zh_sys_prompt
    assert "### 影响评估" in zh_sys_prompt
    assert "Simplified Chinese" in zh_user_prompt

    # Switching system setting to en-US -> system & user prompts must require English + Markdown.
    await db_session.merge(
        SystemSetting(key="ai_output_language", value_json={"value": "en-US"})
    )
    await db_session.commit()

    en_verdict = json.dumps(
        {
            "verdict": "NEW_ISSUE",
            "matched_issue_id": None,
            "confidence": 0.86,
            "suggested_title": "Null texture dereference in Renderer::SubmitDraw",
            "root_cause_analysis": (
                "### Core Root Cause\n\n`Renderer::SubmitDraw` dereferences a null texture."
            ),
            "reasoning_summary": (
                "1. **Stack Analysis**: Crash in `Renderer.dll!Renderer::SubmitDraw`.\n\n"
                "**Conclusion**: New issue."
            ),
            "evidence_log_snippets": [],
            "key_log_patterns": ["Renderer::SubmitDraw"],
            "suggested_component": "Renderer",
        }
    )
    en_client = FakeOpenAIClient(_investigate_then_verdict(en_verdict))
    en_run = await agent.run_triage(db_session, crash, openai_client=en_client)
    assert en_run.status == "COMPLETED"
    en_sys_prompt = en_client.calls[0]["messages"][0]["content"]
    en_user_prompt = en_client.calls[0]["messages"][1]["content"]
    assert "### Root Cause" in en_sys_prompt
    assert "### Session & Subsequent Impact" in en_sys_prompt
    assert "简体中文" not in en_sys_prompt
    assert "concise English" in en_user_prompt
