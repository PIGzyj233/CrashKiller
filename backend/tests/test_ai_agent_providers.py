"""Unit tests for LLM provider protocols (OpenAI thinking levels, Anthropic, Ollama)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.services.ai_agent import (
    AI_TOOL_DEFINITIONS,
    AITriageAgent,
)
from app.services.storage import StorageManager
from tests._ai_agent_helpers import (
    FakeOpenAIClient,
    _investigate_then_verdict,
    _make_response,
    _seed_crash_with_log,
)


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
