"""Verdict JSON parsing, substance validation, and tool-call serialization helpers."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from app.services.ai_agent.prompts import _INT32_MAX, VALID_VERDICTS

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)

_PLACEHOLDER_ANALYSIS_TEXTS: frozenset[str] = frozenset(
    {
        "needs_review",
        "needs review",
        "needs manual review",
        "需人工复核",
        "需要人工复核",
        "unknown",
        "n/a",
        "none",
        "null",
        "-",
    }
)


def _build_needs_review_result(reason: str) -> dict[str, Any]:
    return {
        "verdict": "NEEDS_REVIEW",
        "matched_issue_id": None,
        "confidence": 0.0,
        "suggested_title": "Needs manual review",
        "root_cause_analysis": reason,
        "reasoning_summary": reason,
        "evidence_log_snippets": [],
        "key_log_patterns": [],
        "suggested_component": None,
    }


def _strip_markdown_fences(content: str) -> str:
    match = _FENCE_RE.match(content or "")
    if match:
        return match.group(1).strip()
    return (content or "").strip()


def _has_substantive_text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(text) and text.lower() not in _PLACEHOLDER_ANALYSIS_TEXTS


def _parse_optional_str_field(
    data: dict[str, Any],
    field_name: str,
    *,
    max_len: int | None = None,
) -> str:
    raw = data.get(field_name)
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"{field_name} must be a string")
    cleaned = raw.strip()
    return cleaned[:max_len] if max_len is not None else cleaned


def _validate_verdict_substance(
    verdict_data: dict[str, Any],
    *,
    crash_evidence_reads: int,
) -> str | None:
    """Validate that a parsed verdict is backed by current-crash tool evidence and real analysis."""
    if crash_evidence_reads <= 0:
        return (
            "AI returned a verdict without successfully reading crash evidence "
            "(overview, stack/threads, or logs) via tools"
        )

    has_root_cause = _has_substantive_text(verdict_data.get("root_cause_analysis"))
    has_reasoning = _has_substantive_text(verdict_data.get("reasoning_summary"))

    if not has_root_cause and not has_reasoning:
        return (
            "Empty AI report: both root_cause_analysis and reasoning_summary are missing "
            "substantive content"
        )

    return None


def _parse_verdict_json(raw_content: str | None) -> dict[str, Any]:
    """Parse and validate strict JSON verdict output from the LLM."""
    if not raw_content or not str(raw_content).strip():
        raise ValueError("Empty LLM response content")

    cleaned = _strip_markdown_fences(str(raw_content))
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("LLM verdict output must be a JSON object")

    raw_verdict = data.get("verdict")
    if not isinstance(raw_verdict, str):
        raise ValueError(f"Invalid verdict: {raw_verdict!r}")
    verdict = raw_verdict.strip().upper()
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"Invalid verdict: {raw_verdict!r}")

    raw_matched_id = data.get("matched_issue_id")
    matched_issue_id: int | None
    if raw_matched_id is None or raw_matched_id == "":
        matched_issue_id = None
    elif isinstance(raw_matched_id, bool):
        raise ValueError("matched_issue_id cannot be boolean")
    else:
        parsed_id = int(raw_matched_id)
        matched_issue_id = parsed_id if 1 <= parsed_id <= _INT32_MAX else None

    raw_conf = data.get("confidence")
    confidence: float | None
    if raw_conf is None or raw_conf == "":
        confidence = None
    elif isinstance(raw_conf, bool):
        raise ValueError("confidence cannot be boolean")
    else:
        try:
            parsed_conf = float(raw_conf)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid confidence value: {raw_conf!r}") from exc
        if not math.isfinite(parsed_conf):
            parsed_conf = 0.0
        confidence = round(max(0.0, min(1.0, parsed_conf)), 4)

    suggested_title = _parse_optional_str_field(data, "suggested_title", max_len=512)
    root_cause_analysis = _parse_optional_str_field(data, "root_cause_analysis")
    reasoning_summary = _parse_optional_str_field(data, "reasoning_summary")

    raw_snippets = data.get("evidence_log_snippets") or []
    snippets: list[dict[str, Any]] = []
    if isinstance(raw_snippets, list):
        for item in raw_snippets:
            if not isinstance(item, dict):
                continue
            try:
                start_l = max(1, int(item.get("start_line", 1)))
                end_l = max(start_l, int(item.get("end_line", start_l)))
            except (TypeError, ValueError):
                continue
            snippets.append(
                {
                    "log_id": str(item.get("log_id") or ""),
                    "start_line": start_l,
                    "end_line": end_l,
                    "relevance": str(item.get("relevance") or ""),
                }
            )

    raw_patterns = data.get("key_log_patterns") or []
    patterns: list[str] = []
    if isinstance(raw_patterns, list):
        patterns = [str(p).strip() for p in raw_patterns if p is not None and str(p).strip()]

    comp = data.get("suggested_component")
    if comp is not None and not isinstance(comp, str):
        raise ValueError("suggested_component must be a string or null")
    suggested_component = comp.strip()[:128] if isinstance(comp, str) and comp.strip() else None

    return {
        "verdict": verdict,
        "matched_issue_id": matched_issue_id,
        "confidence": confidence,
        "suggested_title": suggested_title,
        "root_cause_analysis": root_cause_analysis,
        "reasoning_summary": reasoning_summary,
        "evidence_log_snippets": snippets,
        "key_log_patterns": list(dict.fromkeys(patterns)),
        "suggested_component": suggested_component,
    }


def _serialize_tool_calls_for_message(tool_calls: Any) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for tc in tool_calls or []:
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            serialized.append(
                {
                    "id": str(tc.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(fn.get("name") or ""),
                        "arguments": str(fn.get("arguments") or "{}"),
                    },
                }
            )
        else:
            fn = getattr(tc, "function", None)
            serialized.append(
                {
                    "id": str(getattr(tc, "id", "") or ""),
                    "type": "function",
                    "function": {
                        "name": str(getattr(fn, "name", "") or ""),
                        "arguments": str(getattr(fn, "arguments", "{}") or "{}"),
                    },
                }
            )
    return serialized


def _extract_tool_call_fields(tc: Any) -> tuple[str, str, str]:
    if isinstance(tc, dict):
        tc_id = str(tc.get("id") or "")
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "")
        raw_args = fn.get("arguments")
    else:
        tc_id = str(getattr(tc, "id", "") or "")
        fn = getattr(tc, "function", None)
        name = str(getattr(fn, "name", "") or "")
        raw_args = getattr(fn, "arguments", "{}")
    if isinstance(raw_args, dict):
        args_str = json.dumps(raw_args)
    else:
        args_str = str(raw_args or "{}")
    return tc_id, name, args_str
