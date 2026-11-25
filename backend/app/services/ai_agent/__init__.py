"""OpenAI-compatible ReAct AI triage agent with 8 read-only tools and strict guardrails."""

from __future__ import annotations

from app.services.ai_agent.agent import AITriageAgent, run_crash_triage
from app.services.ai_agent.output_trimming import (
    _as_utc,
    _clip_digest_entries,
    _compact_overview_frame,
    _fit_list_prefix,
    _fit_list_suffix,
    _fit_partial_entry,
    _frame_has_evidence,
    _log_entry_has_text,
    _missing_symbol_has_evidence,
    _payload_has_crash_evidence,
    _prepare_tool_output_for_llm,
    _truncate_tool_output,
)
from app.services.ai_agent.prompts import (
    AI_SYSTEM_PROMPT,
    AI_TOOL_DEFINITIONS,
    CRASH_EVIDENCE_TOOLS,
    TRUNCATION_MARKER,
    VALID_VERDICTS,
    _build_output_language_instructions,
    _build_system_prompt,
)
from app.services.ai_agent.verdict import (
    _build_needs_review_result,
    _extract_tool_call_fields,
    _has_substantive_text,
    _parse_optional_str_field,
    _parse_verdict_json,
    _serialize_tool_calls_for_message,
    _strip_markdown_fences,
    _validate_verdict_substance,
)

__all__ = [
    "AI_SYSTEM_PROMPT",
    "AI_TOOL_DEFINITIONS",
    "AITriageAgent",
    "CRASH_EVIDENCE_TOOLS",
    "TRUNCATION_MARKER",
    "VALID_VERDICTS",
    "_as_utc",
    "_build_needs_review_result",
    "_build_output_language_instructions",
    "_build_system_prompt",
    "_clip_digest_entries",
    "_compact_overview_frame",
    "_extract_tool_call_fields",
    "_fit_list_prefix",
    "_fit_list_suffix",
    "_fit_partial_entry",
    "_frame_has_evidence",
    "_has_substantive_text",
    "_log_entry_has_text",
    "_missing_symbol_has_evidence",
    "_parse_optional_str_field",
    "_parse_verdict_json",
    "_payload_has_crash_evidence",
    "_prepare_tool_output_for_llm",
    "_serialize_tool_calls_for_message",
    "_strip_markdown_fences",
    "_truncate_tool_output",
    "_validate_verdict_substance",
    "run_crash_triage",
]
