"""Structural tool-output trimming and crash-evidence verification helpers."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.db.base import utcnow
from app.services.ai_agent.prompts import CRASH_EVIDENCE_TOOLS, TRUNCATION_MARKER

# Identifier-like fields are delivered whole or omitted; clipping an address or module
# name would hand the model a different value than the one in the report.
_ATOMIC_ENTRY_FIELDS = frozenset(
    {"line_number", "frame_index", "ip", "module", "module_offset", "offset"}
)
# A clipped symbol is sent as a preview so it cannot be mistaken for a full function name.
_CLIPPED_ENTRY_FIELDS = {"text": "text", "function": "function_preview"}


def _as_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return utcnow()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _truncate_tool_output(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= len(TRUNCATION_MARKER):
        return text[:max_chars]
    return text[: max_chars - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def _fit_list_prefix(
    trimmed: dict[str, Any],
    key: str,
    items: list[Any],
    budget: int,
) -> int:
    """Binary-search the largest prefix of items that keeps json.dumps(trimmed) <= budget."""
    lo, hi = 0, len(items)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        trimmed[key] = items[:mid]
        if len(json.dumps(trimmed, default=str)) <= budget:
            lo = mid
        else:
            hi = mid - 1
    trimmed[key] = items[:lo]
    return lo


def _fit_list_suffix(
    trimmed: dict[str, Any],
    key: str,
    items: list[Any],
    budget: int,
) -> int:
    """Binary-search the largest suffix of items that keeps json.dumps(trimmed) <= budget."""
    lo, hi = 0, len(items)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        trimmed[key] = items[-mid:] if mid > 0 else []
        if len(json.dumps(trimmed, default=str)) <= budget:
            lo = mid
        else:
            hi = mid - 1
    trimmed[key] = items[-lo:] if lo > 0 else []
    return lo


def _clip_digest_entries(entries: list[Any], *, max_text_chars: int = 500) -> list[Any]:
    """Clip overly long log lines in digest previews so one mega-line cannot starve siblings."""
    clipped: list[Any] = []
    suffix = "...[LINE_TRUNCATED]"
    keep = max(1, max_text_chars - len(suffix))
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("text"), str):
            text = entry["text"]
            if len(text) > max_text_chars:
                entry = {**entry, "text": text[:keep] + suffix}
        clipped.append(entry)
    return clipped


def _compact_overview_frame(frame: Any) -> Any:
    """Strip bulky register dumps and empty inlines from overview stack frames."""
    if not isinstance(frame, dict):
        return frame
    compact: dict[str, Any] = {}
    for k, v in frame.items():
        if k == "registers":
            continue
        if k == "inlines" and not v:
            continue
        compact[k] = v
    return compact


def _fit_partial_entry(
    root: dict[str, Any],
    container: dict[str, Any],
    key: str,
    entry: dict[str, Any],
    budget: int,
    has_evidence: Callable[[Any], bool],
) -> bool:
    """Append a field-trimmed copy of entry after the kept items in container[key].

    Fields are restored in priority order: identifiers (line numbers, addresses, modules)
    first, then the clippable body (text/function) which takes whatever budget is left,
    then remaining metadata such as timestamps and levels. Identifiers are delivered whole
    or omitted. Clipped function names are labeled as previews. The partial entry is kept
    only if it still carries evidence. Otherwise container[key] retains the fitted prefix.
    """
    kept = list(container.get(key) or [])

    def fits() -> bool:
        return len(json.dumps(root, default=str)) <= budget

    def encoded_len(value: Any) -> int:
        return len(json.dumps(value, default=str))

    candidate: dict[str, Any] = {}
    container[key] = [*kept, candidate]
    if not fits():
        container[key] = kept
        return False

    def priority(item: tuple[str, Any]) -> tuple[int, int]:
        name, value = item
        if name in _ATOMIC_ENTRY_FIELDS:
            return 0, encoded_len(value)
        if name in _CLIPPED_ENTRY_FIELDS:
            return 1, 0
        return 2, encoded_len(value)

    for field, value in sorted(entry.items(), key=priority):
        candidate[field] = value
        if fits():
            continue
        del candidate[field]
        clipped_field = _CLIPPED_ENTRY_FIELDS.get(field)
        if clipped_field is None or not isinstance(value, str) or not value:
            continue
        lo, hi = 0, len(value)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            candidate[clipped_field] = value[:mid]
            if fits():
                lo = mid
            else:
                hi = mid - 1
        if value[:lo].strip():
            candidate[clipped_field] = value[:lo]
        else:
            del candidate[clipped_field]

    if not has_evidence(candidate):
        container[key] = kept
        return False
    return True


def _frame_has_evidence(frame: Any) -> bool:
    """Return True if a delivered stack frame carries a location, symbol, or instruction pointer."""
    if not isinstance(frame, dict):
        return False
    for key in ("function", "module", "file", "module_offset", "ip", "offset"):
        val = frame.get(key)
        if isinstance(val, str) and val.strip():
            return True
        if isinstance(val, int) and not isinstance(val, bool) and key in {"ip", "offset"}:
            return True
    return False


def _log_entry_has_text(entry: Any) -> bool:
    """Return True if a delivered log line entry contains non-empty log text."""
    if isinstance(entry, dict):
        text = entry.get("text")
        return isinstance(text, str) and bool(text.strip())
    if isinstance(entry, str):
        return bool(entry.strip())
    return False


def _missing_symbol_has_evidence(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    return any(
        isinstance(item.get(key), str) and item[key].strip()
        for key in ("debug_file", "code_file", "filename", "module")
    )


def _prepare_tool_output_for_llm(
    tool_name: str,
    raw_out: str,
    max_chars: int,
) -> tuple[str, dict[str, Any] | None]:
    """Trim tool output structurally by complete entries and return (delivered_text, payload)."""
    if max_chars <= 0:
        return "", None
    try:
        parsed = json.loads(raw_out)
    except Exception:
        return _truncate_tool_output(raw_out, max_chars), None

    if not isinstance(parsed, dict):
        return _truncate_tool_output(raw_out, max_chars), None

    if len(raw_out) <= max_chars:
        return raw_out, parsed

    budget = max_chars - len(TRUNCATION_MARKER)
    if budget <= 0:
        return _truncate_tool_output(raw_out, max_chars), None

    list_priority: dict[str, tuple[str, ...]] = {
        "get_crash_overview": (
            "logs",
            "candidate_issues",
            "related_session_crashes",
            "top_frames",
            "missing_symbols",
        ),
        "get_other_threads": ("threads",),
        "get_log_summary": (
            "session_recent_errors",
            "post_session_lines",
            "session_tail_lines",
            "recent_errors",
            "tail_lines",
        ),
        "read_log_lines": ("lines",),
        "read_log_around_time": ("lines",),
        "search_log": ("matches",),
        "search_known_issues": ("issues",),
        "get_issue_detail": ("clusters",),
    }

    suffix_list_keys: frozenset[str] = frozenset(
        {"session_tail_lines", "tail_lines"} if tool_name == "get_log_summary" else set()
    )

    trimmed: dict[str, Any] = {
        k: ([] if isinstance(v, list) else v) for k, v in parsed.items()
    }
    if len(json.dumps(trimmed, default=str)) > budget:
        # Even the empty-list skeleton does not fit; fall back to raw truncation with no
        # structured crash-evidence payload.
        return _truncate_tool_output(raw_out, max_chars), None

    preferred = list_priority.get(tool_name, ())
    ordered_list_keys = [
        *[k for k in preferred if isinstance(parsed.get(k), list)],
        *[k for k, v in parsed.items() if isinstance(v, list) and k not in preferred],
    ]

    # Lists whose first non-fitting entry is field-trimmed into the remaining budget,
    # mapped to the check that the trimmed entry still carries usable evidence.
    partial_evidence_checks: dict[str, Callable[[Any], bool]] = {
        "top_frames": _frame_has_evidence,
        "missing_symbols": _missing_symbol_has_evidence,
        "lines": _log_entry_has_text,
        "matches": _log_entry_has_text,
        "tail_lines": _log_entry_has_text,
        "recent_errors": _log_entry_has_text,
        "session_tail_lines": _log_entry_has_text,
        "session_recent_errors": _log_entry_has_text,
        "post_session_lines": _log_entry_has_text,
    }
    for key in ordered_list_keys:
        items = parsed[key]
        if not items:
            continue
        if key in suffix_list_keys:
            fitted = _fit_list_suffix(trimmed, key, items, budget)
            if fitted >= len(items):
                continue
            next_item = items[len(items) - fitted - 1]
        else:
            fitted = _fit_list_prefix(trimmed, key, items, budget)
            if fitted >= len(items):
                continue
            next_item = items[fitted]
        if (
            tool_name == "get_other_threads"
            and key == "threads"
            and isinstance(next_item, dict)
            and isinstance(next_item.get("frames"), list)
        ):
            # Fit as many frames as possible from the next thread entry, then field-trim
            # the first frame that does not fit so its short fields (ip/module) survive.
            next_thread = dict(next_item)
            next_frames = list(next_thread["frames"])
            next_thread["frames"] = []
            trimmed[key] = [*items[:fitted], next_thread]
            if len(json.dumps(trimmed, default=str)) <= budget:
                lo, hi = 0, len(next_frames)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    next_thread["frames"] = next_frames[:mid]
                    if len(json.dumps(trimmed, default=str)) <= budget:
                        lo = mid
                    else:
                        hi = mid - 1
                next_thread["frames"] = next_frames[:lo]
                if lo < len(next_frames) and isinstance(next_frames[lo], dict):
                    _fit_partial_entry(
                        trimmed,
                        next_thread,
                        "frames",
                        next_frames[lo],
                        budget,
                        _frame_has_evidence,
                    )
            else:
                trimmed[key] = items[:fitted]
        elif key in partial_evidence_checks and isinstance(next_item, dict):
            # Keep the already-fitted prefix and field-trim the next entry (e.g. a long
            # FATAL after a short INFO, or a frame with a huge symbol name).
            _fit_partial_entry(
                trimmed, trimmed, key, next_item, budget, partial_evidence_checks[key]
            )

    delivered_text = json.dumps(trimmed, default=str) + TRUNCATION_MARKER
    return delivered_text, trimmed


def _payload_has_crash_evidence(
    tool_name: str,
    delivered_payload: dict[str, Any] | None,
) -> bool:
    """Check whether the actual structured payload delivered to the LLM contains crash evidence."""
    if tool_name not in CRASH_EVIDENCE_TOOLS or not isinstance(delivered_payload, dict):
        return False
    if delivered_payload.get("error"):
        return False

    if tool_name == "get_crash_overview":
        top_frames = delivered_payload.get("top_frames")
        if isinstance(top_frames, list) and any(_frame_has_evidence(f) for f in top_frames):
            return True
        missing_syms = delivered_payload.get("missing_symbols")
        if isinstance(missing_syms, list) and any(
            _missing_symbol_has_evidence(m) for m in missing_syms
        ):
            return True
        return False

    if tool_name == "get_other_threads":
        threads = delivered_payload.get("threads")
        if not isinstance(threads, list):
            return False
        return any(
            isinstance(thread, dict)
            and isinstance(thread.get("frames"), list)
            and any(_frame_has_evidence(f) for f in thread["frames"])
            for thread in threads
        )

    if tool_name == "get_log_summary":
        for key in (
            "session_recent_errors",
            "session_tail_lines",
            "post_session_lines",
            "recent_errors",
            "tail_lines",
        ):
            entries = delivered_payload.get(key)
            if isinstance(entries, list) and any(_log_entry_has_text(e) for e in entries):
                return True
        return False

    if tool_name in {"read_log_lines", "read_log_around_time"}:
        lines = delivered_payload.get("lines")
        return isinstance(lines, list) and any(_log_entry_has_text(e) for e in lines)

    if tool_name == "search_log":
        matches = delivered_payload.get("matches")
        return isinstance(matches, list) and any(_log_entry_has_text(e) for e in matches)

    return False
