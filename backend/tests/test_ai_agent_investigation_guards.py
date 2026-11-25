"""Unit tests for two-phase investigation and evidence sufficiency guardrails."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    Issue,
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
