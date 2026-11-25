"""ReAct AI triage loop, LLM completion adapters, and entrypoints."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import anthropic
import openai
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import AIAnalysisRun, CrashReport, Project
from app.services import llm_providers, settings_service
from app.services.ai_agent.finalizer import finalize_triage_run
from app.services.ai_agent.output_trimming import (
    _payload_has_crash_evidence,
    _prepare_tool_output_for_llm,
)
from app.services.ai_agent.prompts import AI_TOOL_DEFINITIONS, _build_system_prompt
from app.services.ai_agent.tools import AgentToolsMixin
from app.services.ai_agent.verdict import (
    _build_needs_review_result,
    _extract_tool_call_fields,
    _parse_verdict_json,
    _serialize_tool_calls_for_message,
    _validate_verdict_substance,
)
from app.services.clustering import ClusteringService
from app.services.log_engine import LogEngine
from app.services.storage import StorageManager

logger = logging.getLogger(__name__)


class AITriageAgent(AgentToolsMixin):
    """OpenAI-compatible ReAct tool-calling agent for crash root-cause triage."""

    def __init__(
        self,
        storage: StorageManager,
        settings: Settings | None = None,
        log_engine: LogEngine | None = None,
        clustering_service: ClusteringService | None = None,
    ) -> None:
        self.storage = storage
        self.settings = settings
        self.log_engine = log_engine or LogEngine()
        self.clustering_service = clustering_service or ClusteringService()

    async def _resolve_runtime_config(
        self,
        session: AsyncSession,
        settings_override: dict[str, Any] | None,
    ) -> dict[str, Any]:
        base = self.settings or get_settings()
        effective = await settings_service.get_effective_settings(session, base_settings=base)
        cfg: dict[str, Any] = {
            "llm_provider": effective.llm_provider,
            "llm_base_url": effective.llm_base_url,
            "llm_api_key": effective.llm_api_key,
            "llm_model": effective.llm_model,
            "llm_thinking_level": effective.llm_thinking_level,
            "ai_triage_mode": effective.ai_triage_mode,
            "ai_output_language": effective.ai_output_language,
            "ai_max_steps": effective.ai_max_steps,
            "ai_timeout_seconds": effective.ai_timeout_seconds,
            "ai_tool_output_max_chars": effective.ai_tool_output_max_chars,
            "default_noise_modules": list(effective.default_noise_modules),
            "default_noise_functions": list(effective.default_noise_functions),
        }
        if settings_override:
            for key, value in settings_override.items():
                if value is None:
                    continue
                lower_key = str(key).strip().lower()
                if lower_key in cfg:
                    cfg[lower_key] = value
        try:
            cfg["llm_provider"] = llm_providers.normalize_provider_id(cfg["llm_provider"])
        except ValueError:
            cfg["llm_provider"] = effective.llm_provider
        try:
            cfg["llm_thinking_level"] = llm_providers.normalize_thinking_level(
                cfg["llm_thinking_level"]
            )
        except ValueError:
            cfg["llm_thinking_level"] = effective.llm_thinking_level
        try:
            cfg["ai_output_language"] = settings_service.normalize_output_language(
                cfg["ai_output_language"]
            )
        except ValueError:
            cfg["ai_output_language"] = effective.ai_output_language
        cfg["ai_max_steps"] = max(1, int(cfg["ai_max_steps"]))
        cfg["ai_timeout_seconds"] = max(1, int(cfg["ai_timeout_seconds"]))
        cfg["ai_tool_output_max_chars"] = max(50, int(cfg["ai_tool_output_max_chars"]))
        return cfg

    async def _call_completion(
        self,
        client: Any,
        model_name: str,
        messages: list[dict[str, Any]],
        timeout_budget: float,
        *,
        provider_id: str = "openai",
        thinking_level: str = "auto",
        tools: list[dict[str, Any]] | None = AI_TOOL_DEFINITIONS,
        include_response_format: bool = False,
    ) -> Any:
        use_response_format = bool(include_response_format and not tools)
        primary_kwargs = llm_providers.build_openai_completion_kwargs(
            provider_id=provider_id,
            model_name=model_name,
            thinking_level=thinking_level,
            messages=messages,
            tools=tools,
            include_response_format=use_response_format,
            include_thinking=True,
            include_temperature=True,
        )
        try:
            return await asyncio.wait_for(
                client.chat.completions.create(**primary_kwargs),
                timeout=timeout_budget,
            )
        except openai.BadRequestError:
            last_kwargs = primary_kwargs
            if use_response_format:
                # Fallback 1: drop response_format for endpoints disallowing json_object.
                no_rf_kwargs = llm_providers.build_openai_completion_kwargs(
                    provider_id=provider_id,
                    model_name=model_name,
                    thinking_level=thinking_level,
                    messages=messages,
                    tools=tools,
                    include_response_format=False,
                    include_thinking=True,
                    include_temperature=True,
                )
                last_kwargs = no_rf_kwargs
                try:
                    return await asyncio.wait_for(
                        client.chat.completions.create(**no_rf_kwargs),
                        timeout=timeout_budget,
                    )
                except openai.BadRequestError:
                    pass

            # Fallback 2: drop thinking/reasoning params & custom temperature for older models.
            if thinking_level == "auto" and "temperature" not in last_kwargs:
                raise
            safe_kwargs = llm_providers.build_openai_completion_kwargs(
                provider_id=provider_id,
                model_name=model_name,
                thinking_level="auto",
                messages=messages,
                tools=tools,
                include_response_format=False,
                include_thinking=False,
                include_temperature=False,
            )
            return await asyncio.wait_for(
                client.chat.completions.create(**safe_kwargs),
                timeout=timeout_budget,
            )

    async def _call_anthropic_completion(
        self,
        client: Any,
        model_name: str,
        messages: list[dict[str, Any]],
        timeout_budget: float,
        *,
        thinking_level: str = "auto",
        tools: list[dict[str, Any]] | None = AI_TOOL_DEFINITIONS,
    ) -> Any:
        primary_kwargs = llm_providers.build_anthropic_messages_kwargs(
            model_name=model_name,
            thinking_level=thinking_level,
            messages=messages,
            tools=tools,
            include_thinking=True,
        )
        try:
            return await asyncio.wait_for(
                client.messages.create(**primary_kwargs),
                timeout=timeout_budget,
            )
        except anthropic.BadRequestError:
            if thinking_level == "auto":
                raise
            fallback_kwargs = llm_providers.build_anthropic_messages_kwargs(
                model_name=model_name,
                thinking_level="auto",
                messages=messages,
                tools=tools,
                include_thinking=False,
            )
            return await asyncio.wait_for(
                client.messages.create(**fallback_kwargs),
                timeout=timeout_budget,
            )

    async def run_triage(
        self,
        session: AsyncSession,
        crash_report: CrashReport,
        settings_override: dict[str, Any] | None = None,
        openai_client: Any | None = None,
        *,
        anthropic_client: Any | None = None,
        commit: bool = True,
    ) -> AIAnalysisRun:
        """Execute the ReAct tool-calling triage loop and persist verdict + trace."""
        start_mono = time.monotonic()
        runtime_cfg = await self._resolve_runtime_config(session, settings_override)

        provider_id = str(runtime_cfg["llm_provider"])
        thinking_level = str(runtime_cfg["llm_thinking_level"])
        preset = llm_providers.get_provider_preset(provider_id)

        model_name = str(runtime_cfg["llm_model"])
        output_language = str(runtime_cfg["ai_output_language"])
        max_steps = int(runtime_cfg["ai_max_steps"])
        timeout_seconds = int(runtime_cfg["ai_timeout_seconds"])
        tool_max_chars = int(runtime_cfg["ai_tool_output_max_chars"])
        api_key = str(runtime_cfg["llm_api_key"] or "").strip()
        base_url = str(runtime_cfg["llm_base_url"])

        use_anthropic = anthropic_client is not None or (
            preset.protocol == "anthropic_messages" and openai_client is None
        )
        has_injected_client = (
            anthropic_client is not None if use_anthropic else openai_client is not None
        )

        if not has_injected_client and preset.requires_api_key and not api_key:
            result_dict = _build_needs_review_result("LLM_API_KEY is not configured")
            trace = [
                {
                    "step": 1,
                    "role": "system",
                    "error": "LLM_API_KEY is not configured",
                }
            ]
            return await self._finalize_run(
                session=session,
                crash_report=crash_report,
                model_name=model_name,
                run_status="FAILED",
                result_dict=result_dict,
                trace=trace,
                prompt_tokens=0,
                completion_tokens=0,
                duration_ms=max(0, int((time.monotonic() - start_mono) * 1000)),
                commit=commit,
            )

        project = await session.get(Project, crash_report.project_id)
        owns_client = not has_injected_client
        if use_anthropic:
            client: Any = (
                anthropic_client
                if anthropic_client is not None
                else anthropic.AsyncAnthropic(
                    api_key=api_key or "not-required",
                    base_url=llm_providers.normalize_anthropic_base_url(base_url),
                    timeout=float(timeout_seconds),
                    max_retries=0,
                )
            )
        else:
            client = (
                openai_client
                if openai_client is not None
                else openai.AsyncOpenAI(
                    api_key=api_key or "not-required",
                    base_url=base_url,
                    timeout=float(timeout_seconds),
                    max_retries=0,
                )
            )

        trace: list[dict[str, Any]] = []
        prompt_tokens = 0
        completion_tokens = 0
        run_status = "FAILED"
        result_dict: dict[str, Any] | None = None

        session_clause = ""
        if crash_report.session_id:
            label_part = ""
            if project and isinstance(project.settings_json, dict):
                raw_lbl = project.settings_json.get("session_id_label")
                if isinstance(raw_lbl, str) and raw_lbl.strip():
                    label_part = f" [{raw_lbl.strip()}]"
            session_clause = f", session_id{label_part}={crash_report.session_id}"

        language_reminder = (
            "Write `suggested_title`, `root_cause_analysis`, `reasoning_summary`, and snippet "
            "`relevance` in concise Simplified Chinese (简体中文). Keep `root_cause_analysis` "
            "brief (260-450 chars, 3 Markdown sections: 根因与触发原因 / 影响评估"
            " [当次会话影响 & 后续会话影响] / 修复建议, minimal inline code), keep "
            "`reasoning_summary` to 3-4 short non-repetitive bullets (<=200 chars), and limit "
            "`key_log_patterns` to 2-5 core patterns."
            if output_language == "zh-CN"
            else "Write `suggested_title`, `root_cause_analysis`, `reasoning_summary`, and snippet "
            "`relevance` in concise English. Keep `root_cause_analysis` brief (130-230 words, "
            "3 Markdown sections: Root Cause / Session & Subsequent Impact / Recommended Actions, "
            "minimal inline code), keep `reasoning_summary` to 3-4 short non-repetitive bullets "
            "(<=100 words), and limit `key_log_patterns` to 2-5 core patterns."
        )
        user_prompt = (
            f"Triage CrashReport {crash_report.id} "
            f"(app_version={crash_report.app_version}, "
            f"crash_reason={crash_report.crash_reason or 'UNKNOWN'}{session_clause}). "
            "Start by calling get_crash_overview to inspect stack traces, attached logs, "
            "session metadata, and candidate known issues. After completing your investigation, "
            f"output ONLY the final JSON verdict. {language_reminder}"
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _build_system_prompt(output_language)},
            {"role": "user", "content": user_prompt},
        ]

        deadline = start_mono + float(timeout_seconds)

        async def _run_loop() -> None:
            nonlocal prompt_tokens, completion_tokens, run_status, result_dict
            crash_evidence_reads = 0
            investigation_nudged = False
            json_finalization_requested = False

            async def _invoke_llm(
                *,
                allow_tools: bool,
                force_json: bool,
            ) -> tuple[str | None, str | None, Any, dict[str, Any]]:
                nonlocal prompt_tokens, completion_tokens
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("AI triage exceeded AI_TIMEOUT_SECONDS budget")

                step_tools = AI_TOOL_DEFINITIONS if allow_tools else None
                reasoning_content: str | None = None
                if use_anthropic:
                    resp = await self._call_anthropic_completion(
                        client,
                        model_name,
                        messages,
                        remaining,
                        thinking_level=thinking_level,
                        tools=step_tools,
                    )
                    parsed = llm_providers.parse_anthropic_response(resp)
                    prompt_tokens += int(parsed["prompt_tokens"])
                    completion_tokens += int(parsed["completion_tokens"])
                    msg_content = parsed["content"]
                    reasoning_content = parsed["reasoning_content"]
                    tool_calls = parsed["tool_calls"]
                    assistant_msg: dict[str, Any] = {
                        "role": "assistant",
                        "content": msg_content,
                        "_anthropic_content_blocks": parsed["raw_content_blocks"],
                    }
                    if tool_calls:
                        assistant_msg["tool_calls"] = tool_calls
                else:
                    resp = await self._call_completion(
                        client,
                        model_name,
                        messages,
                        remaining,
                        provider_id=provider_id,
                        thinking_level=thinking_level,
                        tools=step_tools,
                        include_response_format=force_json and not allow_tools,
                    )
                    usage = getattr(resp, "usage", None)
                    if usage is not None:
                        prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
                        completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)

                    choices = getattr(resp, "choices", None) or []
                    if not choices:
                        raise ValueError("LLM response contained no choices")
                    msg = choices[0].message
                    msg_content = getattr(msg, "content", None)
                    raw_reasoning = getattr(msg, "reasoning_content", None) or getattr(
                        msg, "reasoning", None
                    )
                    if isinstance(raw_reasoning, str) and raw_reasoning.strip():
                        reasoning_content = raw_reasoning.strip()
                    tool_calls = getattr(msg, "tool_calls", None)

                    assistant_msg = {
                        "role": "assistant",
                        "content": msg_content,
                    }
                    if reasoning_content:
                        assistant_msg["reasoning_content"] = reasoning_content
                    if tool_calls:
                        assistant_msg["tool_calls"] = _serialize_tool_calls_for_message(tool_calls)

                return msg_content, reasoning_content, tool_calls, assistant_msg

            for _step_idx in range(1, max_steps + 1):
                allow_tools = not json_finalization_requested
                force_json = json_finalization_requested
                msg_content, reasoning_content, tool_calls, assistant_msg = await _invoke_llm(
                    allow_tools=allow_tools,
                    force_json=force_json,
                )

                messages.append(assistant_msg)
                if reasoning_content:
                    trace.append(
                        {
                            "step": len(trace) + 1,
                            "role": "reasoning",
                            "output_preview": reasoning_content[:1500],
                        }
                    )

                if allow_tools and tool_calls:
                    for tc in tool_calls:
                        tc_id, tool_name, args_str = _extract_tool_call_fields(tc)
                        t0 = time.monotonic()
                        try:
                            parsed_args = json.loads(args_str) if args_str.strip() else {}
                            if not isinstance(parsed_args, dict):
                                raise ValueError("Tool arguments must be a JSON object")
                        except Exception as exc:
                            parsed_args = {"_raw": args_str}
                            raw_out = json.dumps({"error": f"Invalid JSON arguments: {exc}"})
                            tool_err: str | None = f"Invalid JSON arguments: {exc}"
                        else:
                            raw_out, tool_err = await self._execute_tool(
                                session=session,
                                crash_report=crash_report,
                                project=project,
                                runtime_cfg=runtime_cfg,
                                tool_name=tool_name,
                                args=parsed_args,
                            )

                        tool_duration_ms = max(0, int((time.monotonic() - t0) * 1000))
                        truncated_out, delivered_payload = _prepare_tool_output_for_llm(
                            tool_name, raw_out, tool_max_chars
                        )
                        if tool_err is None and _payload_has_crash_evidence(
                            tool_name, delivered_payload
                        ):
                            crash_evidence_reads += 1
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": truncated_out,
                            }
                        )
                        trace.append(
                            {
                                "step": len(trace) + 1,
                                "role": "tool",
                                "tool_name": tool_name,
                                "arguments": parsed_args,
                                "output_preview": truncated_out[:1000],
                                "error": tool_err,
                                "duration_ms": tool_duration_ms,
                            }
                        )
                    continue

                # Non-tool message (or JSON finalization response): parse and validate verdict.
                parsed_verdict: dict[str, Any] | None = None
                failure_reason: str | None = None
                try:
                    parsed_verdict = _parse_verdict_json(msg_content)
                except Exception as exc:
                    failure_reason = f"Malformed LLM JSON verdict: {exc}"
                else:
                    failure_reason = _validate_verdict_substance(
                        parsed_verdict,
                        crash_evidence_reads=crash_evidence_reads,
                    )

                if parsed_verdict is not None and failure_reason is None:
                    result_dict = parsed_verdict
                    run_status = "COMPLETED"
                    return

                assert failure_reason is not None

                # Recovery 1: Current crash evidence has not been read yet; nudge once to inspect.
                if (
                    crash_evidence_reads == 0
                    and not investigation_nudged
                    and _step_idx < max_steps
                ):
                    investigation_nudged = True
                    trace.append(
                        {
                            "step": len(trace) + 1,
                            "role": "system",
                            "message": (
                                "Model responded before successfully reading current crash "
                                "overview/stack/logs via tools; prompting it to call "
                                "get_crash_overview first."
                            ),
                            "output_preview": str(msg_content or "")[:1000],
                        }
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"Your previous response was not accepted ({failure_reason}). "
                                "You have not successfully read the current crash overview, "
                                "stack trace, or logs yet. Call get_crash_overview first (and "
                                "inspect attached logs or other threads as needed) before "
                                "producing your final JSON verdict."
                            ),
                        }
                    )
                    continue

                # Recovery 2: Crash evidence was read, but output was prose or incomplete JSON.
                # Transition to Phase 2: disable tools and enable strict JSON output format.
                if crash_evidence_reads > 0 and not json_finalization_requested:
                    json_finalization_requested = True
                    trace.append(
                        {
                            "step": len(trace) + 1,
                            "role": "system",
                            "message": (
                                "Investigation completed; requesting structured JSON verdict "
                                "without tools."
                            ),
                            "output_preview": str(msg_content or "")[:1000],
                        }
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"Your investigation is complete, but your last message could not "
                                f"be accepted as the final structured verdict ({failure_reason}). "
                                "Now output ONLY a single valid JSON object grounded strictly in "
                                "the tool results above. Include all fields: verdict "
                                "(KNOWN_ISSUE | NEW_ISSUE | REGRESSION | NEEDS_REVIEW), "
                                "matched_issue_id (integer or null), confidence (number 0.0-1.0), "
                                "suggested_title, root_cause_analysis, reasoning_summary, "
                                "evidence_log_snippets, key_log_patterns, and suggested_component. "
                                "All text fields must be strings, and root_cause_analysis or "
                                f"reasoning_summary must not be empty. {language_reminder}"
                            ),
                        }
                    )
                    if _step_idx < max_steps:
                        continue
                    final_content, final_reasoning, _final_tc, final_msg = await _invoke_llm(
                        allow_tools=False,
                        force_json=True,
                    )
                    messages.append(final_msg)
                    if final_reasoning:
                        trace.append(
                            {
                                "step": len(trace) + 1,
                                "role": "reasoning",
                                "output_preview": final_reasoning[:1500],
                            }
                        )
                    try:
                        final_verdict = _parse_verdict_json(final_content)
                    except Exception as exc:
                        failure_reason = f"Malformed LLM JSON verdict: {exc}"
                        msg_content = final_content
                    else:
                        final_err = _validate_verdict_substance(
                            final_verdict,
                            crash_evidence_reads=crash_evidence_reads,
                        )
                        if final_err is None:
                            result_dict = final_verdict
                            run_status = "COMPLETED"
                            return
                        failure_reason = final_err
                        msg_content = final_content

                trace.append(
                    {
                        "step": len(trace) + 1,
                        "role": "system",
                        "error": failure_reason,
                        "output_preview": str(msg_content or "")[:1000],
                    }
                )
                result_dict = _build_needs_review_result(failure_reason)
                run_status = "FAILED"
                return

            # Exhausted max_steps while calling tools. If current-crash evidence was already
            # retrieved, grant one tools-disabled JSON summary turn within the remaining timeout
            # budget so configurations like AI_MAX_STEPS=1 can still produce a final verdict.
            post_budget_preview: str | None = None
            post_budget_error: str | None = None
            if crash_evidence_reads > 0:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"You have reached the investigation step limit ({max_steps}). "
                            "No further tool calls are available. Based strictly on the crash "
                            "evidence already gathered above, output ONLY a single valid JSON "
                            "object with all required verdict fields (verdict, matched_issue_id, "
                            "confidence, suggested_title, root_cause_analysis, reasoning_summary, "
                            "evidence_log_snippets, key_log_patterns, suggested_component). "
                            "All text fields must be strings, and root_cause_analysis or "
                            f"reasoning_summary must not be empty. {language_reminder}"
                        ),
                    }
                )
                final_content, final_reasoning, _final_tc, final_msg = await _invoke_llm(
                    allow_tools=False,
                    force_json=True,
                )
                messages.append(final_msg)
                if final_reasoning:
                    trace.append(
                        {
                            "step": len(trace) + 1,
                            "role": "reasoning",
                            "output_preview": final_reasoning[:1500],
                        }
                    )
                if final_content and str(final_content).strip():
                    post_budget_preview = str(final_content)[:1000]
                try:
                    final_verdict = _parse_verdict_json(final_content)
                except Exception as exc:
                    if post_budget_preview is not None:
                        post_budget_error = f"Malformed LLM JSON verdict: {exc}"
                else:
                    final_err = _validate_verdict_substance(
                        final_verdict,
                        crash_evidence_reads=crash_evidence_reads,
                    )
                    if final_err is None:
                        result_dict = final_verdict
                        run_status = "COMPLETED"
                        return
                    post_budget_error = final_err

            # Exhausted max_steps without a valid final verdict.
            failure_reason = (
                post_budget_error
                if post_budget_error is not None
                else f"Exceeded AI_MAX_STEPS ({max_steps}) without final verdict"
            )
            limit_entry: dict[str, Any] = {
                "step": len(trace) + 1,
                "role": "system",
                "error": failure_reason,
            }
            if post_budget_preview:
                limit_entry["output_preview"] = post_budget_preview
            trace.append(limit_entry)
            result_dict = _build_needs_review_result(failure_reason)
            run_status = "FAILED"

        try:
            await asyncio.wait_for(_run_loop(), timeout=float(timeout_seconds))
        except TimeoutError:
            timeout_reason = f"AI triage timed out after {timeout_seconds}s"
            trace.append(
                {
                    "step": len(trace) + 1,
                    "role": "system",
                    "error": timeout_reason,
                }
            )
            result_dict = _build_needs_review_result(timeout_reason)
            run_status = "FAILED"
        except Exception as exc:
            logger.warning(
                "AI triage error; degrading safely to NEEDS_REVIEW: %s",
                exc,
                exc_info=True,
            )
            err_reason = f"AI triage failed: {exc or type(exc).__name__}"
            trace.append(
                {
                    "step": len(trace) + 1,
                    "role": "system",
                    "error": err_reason,
                }
            )
            result_dict = _build_needs_review_result(err_reason)
            run_status = "FAILED"
        finally:
            if owns_client:
                close_fn = getattr(client, "close", None)
                if callable(close_fn):
                    try:
                        await close_fn()
                    except Exception:
                        pass

        assert result_dict is not None
        duration_ms = max(0, int((time.monotonic() - start_mono) * 1000))
        return await self._finalize_run(
            session=session,
            crash_report=crash_report,
            model_name=model_name,
            run_status=run_status,
            result_dict=result_dict,
            trace=trace,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=duration_ms,
            commit=commit,
        )

    async def _finalize_run(
        self,
        *,
        session: AsyncSession,
        crash_report: CrashReport,
        model_name: str,
        run_status: str,
        result_dict: dict[str, Any],
        trace: list[dict[str, Any]],
        prompt_tokens: int,
        completion_tokens: int,
        duration_ms: int,
        commit: bool = True,
    ) -> AIAnalysisRun:
        return await finalize_triage_run(
            session=session,
            crash_report=crash_report,
            model_name=model_name,
            run_status=run_status,
            result_dict=result_dict,
            trace=trace,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=duration_ms,
            commit=commit,
        )


async def run_crash_triage(
    session: AsyncSession,
    crash_report: CrashReport,
    storage: StorageManager,
    settings: Settings | None = None,
    settings_override: dict[str, Any] | None = None,
    openai_client: Any | None = None,
    anthropic_client: Any | None = None,
) -> AIAnalysisRun:
    """Convenience wrapper executing AITriageAgent.run_triage for workers and callers."""
    agent = AITriageAgent(storage=storage, settings=settings)
    return await agent.run_triage(
        session=session,
        crash_report=crash_report,
        settings_override=settings_override,
        openai_client=openai_client,
        anthropic_client=anthropic_client,
    )
