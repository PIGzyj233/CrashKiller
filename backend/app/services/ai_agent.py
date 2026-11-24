"""OpenAI-compatible ReAct AI triage agent with 8 read-only tools and strict guardrails."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import anthropic
import openai
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.base import utcnow
from app.db.models import (
    AIAnalysisRun,
    CrashCluster,
    CrashLog,
    CrashReport,
    Issue,
    Project,
)
from app.services import llm_providers, settings_service
from app.services.clustering import ClusteringService, _compare_versions
from app.services.log_engine import LogEngine, normalize_session_id
from app.services.storage import StorageError, StorageManager

logger = logging.getLogger(__name__)

VALID_VERDICTS: frozenset[str] = frozenset(
    {"KNOWN_ISSUE", "NEW_ISSUE", "REGRESSION", "NEEDS_REVIEW"}
)
CRASH_EVIDENCE_TOOLS: frozenset[str] = frozenset(
    {
        "get_crash_overview",
        "get_other_threads",
        "get_log_summary",
        "read_log_lines",
        "read_log_around_time",
        "search_log",
    }
)
TRUNCATION_MARKER = "\n...[TRUNCATED]"
_INT32_MAX = 2_147_483_647

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)

_AI_SYSTEM_PROMPT_BASE = """You are a senior C++ Windows/Native Crash Triage Engineer.
You investigate a crash dump and its diagnostic logs using ONLY the provided read-only tools.

Investigation rules:
1. ALWAYS start by calling get_crash_overview to inspect crashing stack frames, missing symbols,
   session_id (if present), related_session_crashes, candidate known issues, and attached logs.
2. Inspect attached logs (get_log_summary, search_log, read_log_around_time, read_log_lines),
   other threads (get_other_threads), or known issues (search_known_issues, get_issue_detail).
3. CRITICAL session & mid-file crash log rule:
   - Processes often auto-restart after crashing, and logs may be collected manually hours later.
     Therefore, the crashing session often lives in the MIDDLE of a log file rather than the end.
   - When session_id is present (especially if trailing_lines_after_session > 0), DO NOT rely on
     global tail_lines or global recent_errors from post-restart sessions at the end of the file.
   - Prioritize session_recent_errors, session_tail_lines, and post_session_lines (untagged
     crash-moment lines and restart boundary right after session_last_line) from get_log_summary.
   - Use read_log_lines around session_last_line (e.g. [session_last_line - 80, +40]) to inspect
     untagged fatal/exception lines around the crash boundary, and pass only_session=true to
     search_log or read_log_around_time to filter out other sessions.
   - On delayed manual uploads, crash_time may reflect upload time rather than actual crash time;
     anchor to session_last_line / session_last_ts, and check related_session_crashes too.
4. Never invent log line numbers, file contents, or issue_id values. Cite only observed tool data.
   Use function_preview only as a hint from a truncated function name.
   Never return an empty NEEDS_REVIEW without first calling tools to inspect the crash.
5. Only AFTER completing your tool-based investigation, output ONLY a single valid JSON object
   (no surrounding prose) with non-empty analysis fields (including for NEEDS_REVIEW):
{
  "verdict": "KNOWN_ISSUE | NEW_ISSUE | REGRESSION | NEEDS_REVIEW",
  "matched_issue_id": 42,
  "confidence": 0.91,
  "suggested_title": "concise plain-text issue title in the required output language",
  "root_cause_analysis": "structured Markdown analysis (headings, lists, inline code)",
  "reasoning_summary": "structured Markdown numbered reasoning steps and conclusion",
  "evidence_log_snippets": [
    {"log_id": "uuid", "start_line": 1, "end_line": 5, "relevance": "why this matters"}
  ],
  "key_log_patterns": ["distinctive literal error substring from logs or stack"],
  "suggested_component": "subsystem name or null"
}
"""


def _build_output_language_instructions(output_language: str) -> str:
    """Return strict language, brevity, and impact-focused Markdown rules."""
    if output_language == "en-US":
        return (
            "\n6. Output language, brevity & impact-focused Markdown rules (STRICT — optimize "
            "for 10-second human scanning, NOT an essay):\n"
            "   - Write ALL human-readable fields (`suggested_title`, `root_cause_analysis`, "
            "`reasoning_summary`, and `evidence_log_snippets[].relevance`) in clear, concise "
            "English.\n"
            "   - `suggested_title`: one short plain-text title (<= 14 words, no Markdown). "
            "State scenario + core error cleanly.\n"
            "   - `suggested_component`: short subsystem name (<= 4 words, e.g. "
            "`DXGI / WGC Capture`), never a full sentence.\n"
            "   - `root_cause_analysis`: rendered as Markdown in the main card. Keep total length "
            "tight (~130-230 words, ~10-15 lines). DO NOT recite the full call stack or paste raw "
            "log lines (the UI already displays the stack and clickable log cards). Answer what "
            "engineers care about most in exactly 3 concise sections separated by `\\n\\n`:\n"
            "     `### Root Cause`\\n(2-3 direct sentences explaining why it crashed, the root "
            "cause, and the single decisive stack/log fact)\\n\\n"
            "`### Session & Subsequent Impact`\\n"
            "- **Current Session**: (Did this interrupt/hang/kill the active session, or was it "
            "recovered? 1 concise sentence grounded in logs/dump.)\\n"
            "- **Subsequent Sessions**: (Based on post-crash restart/trailing logs, did the "
            "process restart cleanly and resume service, or is there lingering GPU/driver/state "
            "risk? State explicitly if log evidence is insufficient.)\\n\\n"
            "`### Recommended Actions`\\n- (2-3 concrete, 1-sentence engineering fixes)\n"
            "   - Use inline backticks sparingly (1-2 key symbols/error codes per sentence). Do "
            "NOT wrap entire log lines, timestamps, or internal JSON field names in backticks.\n"
            "   - `reasoning_summary`: shown in a secondary collapsible panel. Keep to 3-4 brief "
            "bullets (<= 100 words total) explaining ONLY why the verdict/issue match was chosen "
            "and cross-checks performed. DO NOT repeat `root_cause_analysis`.\n"
            "   - `evidence_log_snippets[].relevance`: 1 concise sentence (<= 20 words) per item.\n"
            "   - `key_log_patterns`: keep strictly to 2-5 high-signal searchable error substrings "
            "(never dump 10+ generic stack frame names).\n"
        )
    return (
        "\n6. 输出语言、精炼度与影响评估规范（必须严格遵守，让工程师 10 秒内看懂，"
        "严禁写成高密度长文）：\n"
        "   - 所有面向人类阅读的字段（`suggested_title`、`root_cause_analysis`、"
        "`reasoning_summary`、`evidence_log_snippets[].relevance`）必须使用**简体中文**撰写。\n"
        "   - `suggested_title`：一行精炼中文标题（不超过 40 字，无 Markdown 标记），"
        "省去冗长命名空间，直接说明“触发场景 + 核心错误”。\n"
        "   - `suggested_component`：简短子系统名（不超过 20 字，如 `DXGI/WGC 显存采集`），"
        "不要写整句带括号和箭头的长描述。\n"
        "   - `root_cause_analysis`：前端主卡片按 Markdown 渲染。**严格控制总篇幅在 260~450 字"
        "（约 10~15 行）**，**禁止复述完整调用栈或抄写整行原始日志**（页面下方已有完整调用栈和"
        "可点击的证据日志卡片）。围绕工程师最关心的两个核心问题，固定使用以下 3 个精炼小节"
        "（小节间用 `\\n\\n` 分隔）：\n"
        "     `### 根因与触发原因`\\n（2~3 句直奔主题：为什么崩溃、根本原因是什么，带出 1 个最核心"
        "业务入口与 1 个决定性错误码/日志事实即可）\\n\\n"
        "`### 影响评估`\\n"
        "- **当次会话影响**：（明确结论：是否中断/卡死/强杀了当次会话，还是容错继续运行，1 句话给出"
        "日志或堆栈依据）\\n"
        "- **后续会话影响**：（结合 `post_session_lines` 与崩溃后日志，明确说明进程是否正常自动"
        "重启并恢复服务、是否存在 GPU/驱动异常或脏状态延续到下一次会话的风险；若无后续日志则如实"
        "注明“无崩溃后日志，暂无法确认”）\\n\\n"
        "`### 修复建议`\\n- （2~3 条可直接落地的短动作，每条 1 句话，不展开长篇说教）\n"
        "   - **克制使用行内代码反引号**：每句仅对 1~2 个最关键的函数名/错误码/文件名加反引号，"
        "**禁止把整条日志、时间戳、内部字段名或所有参数都包成代码胶囊**，避免满屏灰块干扰阅读。\n"
        "   - `reasoning_summary`：在前端默认折叠的「推理依据」区域展示。仅用 **3~4 条极简短句"
        "列表（总字数不超过 200 字）**说明为何做出该 `verdict` 判定（如命中哪个已知问题、关键"
        "交叉验证结果），**严禁把 `root_cause_analysis` 的内容再重复写一遍**。\n"
        "   - `evidence_log_snippets[].relevance`：每条限 1 句精炼中文（不超过 45 字），直接"
        "说明该段日志证明了什么。\n"
        "   - `key_log_patterns`：严格精选 **2~5 条**最具检索价值的核心错误特征子串，"
        "不要塞入十余个普通函数名或泛用词。\n"
    )


def _build_system_prompt(output_language: str = "zh-CN") -> str:
    return _AI_SYSTEM_PROMPT_BASE + _build_output_language_instructions(output_language)


AI_SYSTEM_PROMPT = _build_system_prompt("zh-CN")

AI_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_crash_overview",
            "description": (
                "Get crash metadata (including custom session_id if present), top frames, "
                "missing symbols, cluster info, related crashes in the same session, recalled "
                "candidate issues, and attached log metadata (including mid-file session spans)."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_other_threads",
            "description": (
                "Inspect non-crashing threads from the minidump, optionally filtered by keyword."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": (
                            "Optional case-insensitive filter matching thread_name, "
                            "module, or function."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_log_summary",
            "description": (
                "Get precomputed log digest (error/warn counts, tail lines, recent errors, plus "
                "session errors/tail and post_session_lines boundary context if session_id is set)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "log_id": {
                        "type": "string",
                        "description": "UUID of the CrashLog belonging to this crash.",
                    },
                },
                "required": ["log_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_log_lines",
            "description": (
                "Read a specific 1-based line range from an attached log "
                "(hard capped at 200 lines)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "log_id": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["log_id", "start_line", "end_line"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_log_around_time",
            "description": (
                "Read log lines within a time window around a target timestamp "
                "(defaults to crash_time; when only_session=true and default crash_time misses "
                "on delayed uploads, automatically falls back to session_last_ts)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "log_id": {"type": "string"},
                    "timestamp": {
                        "type": "string",
                        "description": (
                            "ISO-8601 or YYYY-MM-DD HH:MM:SS timestamp; "
                            "defaults to crash_time if omitted."
                        ),
                    },
                    "window_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 120,
                        "default": 10,
                    },
                    "max_lines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 200,
                    },
                    "only_session": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, restrict results to lines containing the crash's session_id."
                        ),
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional explicit session_id filter override.",
                    },
                },
                "required": ["log_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_log",
            "description": (
                "Search an attached compressed log for a substring or regex pattern, "
                "optionally restricted to lines belonging to the crash's session_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "log_id": {"type": "string"},
                    "pattern": {"type": "string"},
                    "is_regex": {"type": "boolean", "default": False},
                    "max_matches": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 50,
                    },
                    "case_sensitive": {"type": "boolean", "default": False},
                    "only_session": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, only return matches on lines containing this crash's "
                            "session_id."
                        ),
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional explicit session_id filter override.",
                    },
                },
                "required": ["log_id", "pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_known_issues",
            "description": (
                "Search project knowledge base issues using stack similarity and "
                "optional text/module/function query."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "default": ""},
                    "module": {"type": "string"},
                    "function": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_issue_detail",
            "description": (
                "Fetch full details and linked stack signatures of a project issue by ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_id": {"type": "integer"},
                },
                "required": ["issue_id"],
                "additionalProperties": False,
            },
        },
    },
]


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


# Identifier-like fields are delivered whole or omitted; clipping an address or module
# name would hand the model a different value than the one in the report.
_ATOMIC_ENTRY_FIELDS = frozenset(
    {"line_number", "frame_index", "ip", "module", "module_offset", "offset"}
)
# A clipped symbol is sent as a preview so it cannot be mistaken for a full function name.
_CLIPPED_ENTRY_FIELDS = {"text": "text", "function": "function_preview"}


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


class AITriageAgent:
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

    async def _load_crash_log(
        self,
        session: AsyncSession,
        crash_id: uuid.UUID,
        log_id_raw: Any,
    ) -> tuple[CrashLog | None, str | None]:
        try:
            log_uuid = uuid.UUID(str(log_id_raw))
        except (TypeError, ValueError):
            return None, "Invalid log_id UUID"

        row = (
            await session.execute(
                select(CrashLog).where(
                    CrashLog.id == log_uuid,
                    CrashLog.crash_id == crash_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None, "Log not found"
        return row, None

    async def _execute_tool(
        self,
        session: AsyncSession,
        crash_report: CrashReport,
        project: Project | None,
        runtime_cfg: dict[str, Any],
        tool_name: str,
        args: dict[str, Any],
    ) -> tuple[str, str | None]:
        """Dispatch one of the 8 read-only tools and return (json_str, error_str)."""
        try:
            if tool_name == "get_crash_overview":
                payload = await self._tool_get_crash_overview(
                    session, crash_report, project, runtime_cfg
                )
            elif tool_name == "get_other_threads":
                payload = self._tool_get_other_threads(crash_report, args)
            elif tool_name == "get_log_summary":
                payload = await self._tool_get_log_summary(session, crash_report, args)
            elif tool_name == "read_log_lines":
                payload = await self._tool_read_log_lines(session, crash_report, args)
            elif tool_name == "read_log_around_time":
                payload = await self._tool_read_log_around_time(session, crash_report, args)
            elif tool_name == "search_log":
                payload = await self._tool_search_log(session, crash_report, args)
            elif tool_name == "search_known_issues":
                payload = await self._tool_search_known_issues(
                    session, crash_report, project, runtime_cfg, args
                )
            elif tool_name == "get_issue_detail":
                payload = await self._tool_get_issue_detail(session, crash_report, args)
            else:
                err_msg = f"Unknown tool: {tool_name}"
                return json.dumps({"error": err_msg}), err_msg

            err = payload.get("error") if isinstance(payload, dict) else None
            return json.dumps(payload, default=str), (str(err) if err else None)
        except Exception as exc:
            logger.warning("Tool %s failed cleanly: %s", tool_name, exc, exc_info=True)
            err_msg = str(exc) or type(exc).__name__
            return json.dumps({"error": err_msg}), err_msg

    async def _tool_get_crash_overview(
        self,
        session: AsyncSession,
        crash_report: CrashReport,
        project: Project | None,
        runtime_cfg: dict[str, Any],
    ) -> dict[str, Any]:
        stacks = crash_report.parsed_stacks_json or {}
        threads = stacks.get("threads") or []
        try:
            crashing_idx = int(stacks.get("crashing_thread_index") or 0)
        except (TypeError, ValueError):
            crashing_idx = 0

        top_frames: list[dict[str, Any]] = []
        if (
            threads
            and 0 <= crashing_idx < len(threads)
            and isinstance(threads[crashing_idx], dict)
        ):
            raw_top_frames = list((threads[crashing_idx].get("frames") or [])[:30])
            top_frames = [_compact_overview_frame(f) for f in raw_top_frames]
        elif crash_report.parsed_summary_json:
            raw_top_frames = list((crash_report.parsed_summary_json.get("top_frames") or [])[:30])
            top_frames = [_compact_overview_frame(f) for f in raw_top_frames]

        cluster_info: dict[str, Any] | None = None
        if crash_report.cluster_id is not None:
            cluster = await session.get(CrashCluster, crash_report.cluster_id)
            if cluster is not None:
                cluster_info = {
                    "id": str(cluster.id),
                    "signature_hash": cluster.signature_hash,
                    "issue_id": cluster.issue_id,
                    "crash_count": cluster.crash_count,
                }

        noise_rules = self.clustering_service.resolve_noise_rules(
            project.settings_json if project else None,
            runtime_cfg,
        )
        normalized_frames = self.clustering_service.filter_and_normalize_frames(
            stacks,
            noise_rules,
            crash_reason=crash_report.crash_reason,
        )
        candidate_issues = await self.clustering_service.recall_similar_issues(
            session,
            crash_report.project_id,
            normalized_frames[:10],
            limit=5,
        )

        logs_rows = (
            await session.execute(
                select(CrashLog).where(CrashLog.crash_id == crash_report.id)
            )
        ).scalars().all()
        logs_list: list[dict[str, Any]] = []
        for log in logs_rows:
            digest = log.digest_json or {}
            log_item: dict[str, Any] = {
                "id": str(log.id),
                "filename": log.filename,
                "raw_size": log.raw_size,
                "total_lines": log.total_lines,
                "error_count": int(digest.get("error_count", 0)),
                "warn_count": int(digest.get("warn_count", 0)),
            }
            sess_digest = digest.get("session")
            if isinstance(sess_digest, dict):
                log_item["session_match_count"] = int(sess_digest.get("match_count", 0))
                log_item["session_error_count"] = int(sess_digest.get("error_count", 0))
                log_item["session_warn_count"] = int(sess_digest.get("warn_count", 0))
                log_item["session_first_line"] = sess_digest.get("first_line")
                log_item["session_last_line"] = sess_digest.get("last_line")
                log_item["session_first_ts"] = sess_digest.get("first_ts")
                log_item["session_last_ts"] = sess_digest.get("last_ts")
                log_item["trailing_lines_after_session"] = int(
                    sess_digest.get("trailing_lines_after_session", 0)
                )
            logs_list.append(log_item)

        session_id_label: str | None = None
        if project and isinstance(project.settings_json, dict):
            raw_label = project.settings_json.get("session_id_label")
            if isinstance(raw_label, str) and raw_label.strip():
                session_id_label = raw_label.strip()

        related_session_crashes: list[dict[str, Any]] = []
        related_session_crash_count = 0
        if crash_report.session_id:
            rel_filters = (
                CrashReport.project_id == crash_report.project_id,
                CrashReport.session_id == crash_report.session_id,
                CrashReport.id != crash_report.id,
            )
            related_session_crash_count = int(
                (
                    await session.execute(
                        select(func.count()).select_from(CrashReport).where(*rel_filters)
                    )
                ).scalar_one()
            )
            if related_session_crash_count > 0:
                rel_rows = (
                    await session.execute(
                        select(CrashReport)
                        .where(*rel_filters)
                        .order_by(CrashReport.crash_time.desc(), CrashReport.created_at.desc())
                        .limit(10)
                    )
                ).scalars().all()
                related_session_crashes = [
                    {
                        "crash_id": str(rel.id),
                        "status": rel.status,
                        "app_version": rel.app_version,
                        "crash_reason": rel.crash_reason,
                        "crash_time": _as_utc(rel.crash_time).isoformat(),
                        "issue_id": rel.issue_id,
                        "ai_verdict": rel.ai_verdict,
                    }
                    for rel in rel_rows
                ]

        all_missing = list(crash_report.missing_symbols_json or [])
        crashing_missing_mods: set[str] = set()
        for frame in top_frames:
            if not isinstance(frame, dict):
                continue
            if frame.get("missing_symbols") or (not frame.get("function") and frame.get("module")):
                mod = str(frame.get("module") or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
                if mod:
                    crashing_missing_mods.add(mod)

        crashing_missing: list[Any] = []
        other_missing: list[Any] = []
        for sym in all_missing:
            if isinstance(sym, dict) and crashing_missing_mods:
                candidates = {
                    str(sym.get(field) or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
                    for field in ("module_name", "code_file", "debug_file")
                    if sym.get(field)
                }
                if candidates & crashing_missing_mods:
                    crashing_missing.append(sym)
                    continue
            other_missing.append(sym)
        other_limit = max(0, 10 - len(crashing_missing))
        overview_missing = crashing_missing[:20] + other_missing[:other_limit]

        overview: dict[str, Any] = {
            "crash_id": str(crash_report.id),
            "project_id": str(crash_report.project_id),
            "app_version": crash_report.app_version,
            "build_id": crash_report.build_id,
            "os_version": crash_report.os_version,
            "session_id": crash_report.session_id,
            "crash_reason": crash_report.crash_reason,
            "crash_address": crash_report.crash_address,
            "crash_time": _as_utc(crash_report.crash_time).isoformat(),
            "crashing_thread_index": crashing_idx,
            "top_frames": top_frames,
            "missing_symbol_count": len(all_missing),
            "crashing_stack_missing_symbol_count": len(crashing_missing),
            "missing_symbols": overview_missing,
            "cluster": cluster_info,
            "candidate_issues": candidate_issues,
            "logs": logs_list,
            "related_session_crash_count": related_session_crash_count,
            "related_session_crashes": related_session_crashes,
        }
        if session_id_label:
            overview["session_id_label"] = session_id_label
        return overview

    def _tool_get_other_threads(
        self,
        crash_report: CrashReport,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        stacks = crash_report.parsed_stacks_json or {}
        threads = stacks.get("threads") or []
        try:
            crashing_idx = int(stacks.get("crashing_thread_index") or 0)
        except (TypeError, ValueError):
            crashing_idx = 0

        keyword = str(args.get("keyword") or "").strip().lower()
        matched_threads: list[dict[str, Any]] = []

        for pos, thread in enumerate(threads):
            if not isinstance(thread, dict):
                continue
            raw_t_idx = thread.get("thread_index")
            t_idx = pos if raw_t_idx is None else raw_t_idx
            if t_idx == crashing_idx:
                continue
            frames = list(thread.get("frames") or [])
            if keyword:
                t_name = str(thread.get("thread_name") or "").lower()
                name_hit = keyword in t_name
                frame_hit = any(
                    keyword in str(f.get("module") or "").lower()
                    or keyword in str(f.get("function") or "").lower()
                    or keyword in str(f.get("file") or "").lower()
                    for f in frames
                    if isinstance(f, dict)
                )
                if not name_hit and not frame_hit:
                    continue
            matched_threads.append(
                {
                    "thread_index": t_idx,
                    "thread_id": thread.get("thread_id"),
                    "thread_name": thread.get("thread_name"),
                    "frames": frames[:20],
                }
            )
            if len(matched_threads) >= 20:
                break

        return {
            "crashing_thread_index": crashing_idx,
            "total_other_threads": max(0, len(threads) - 1),
            "threads": matched_threads,
        }

    async def _tool_get_log_summary(
        self,
        session: AsyncSession,
        crash_report: CrashReport,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        log_row, err = await self._load_crash_log(
            session, crash_report.id, args.get("log_id")
        )
        if log_row is None:
            return {"error": err or "Log not found"}

        digest = log_row.digest_json or {}
        summary: dict[str, Any] = {
            "log_id": str(log_row.id),
            "filename": log_row.filename,
            "total_lines": log_row.total_lines,
            "raw_size": log_row.raw_size,
            "compressed_size": log_row.compressed_size,
            "error_count": int(digest.get("error_count", 0)),
            "warn_count": int(digest.get("warn_count", 0)),
            "tail_lines": _clip_digest_entries(list(digest.get("tail_lines") or [])[-25:]),
            "recent_errors": _clip_digest_entries(list(digest.get("recent_errors") or [])[-25:]),
        }
        sess_digest = digest.get("session")
        if isinstance(sess_digest, dict):
            summary["session_id"] = sess_digest.get("session_id")
            summary["session_match_count"] = int(sess_digest.get("match_count", 0))
            summary["session_error_count"] = int(sess_digest.get("error_count", 0))
            summary["session_warn_count"] = int(sess_digest.get("warn_count", 0))
            summary["session_first_line"] = sess_digest.get("first_line")
            summary["session_last_line"] = sess_digest.get("last_line")
            summary["session_first_ts"] = sess_digest.get("first_ts")
            summary["session_last_ts"] = sess_digest.get("last_ts")
            summary["trailing_lines_after_session"] = int(
                sess_digest.get("trailing_lines_after_session", 0)
            )
            summary["session_recent_errors"] = _clip_digest_entries(
                list(sess_digest.get("recent_errors") or [])[-25:]
            )
            summary["session_tail_lines"] = _clip_digest_entries(
                list(sess_digest.get("tail_lines") or [])[-25:]
            )
            summary["post_session_lines"] = _clip_digest_entries(
                list(sess_digest.get("post_session_lines") or [])[:25]
            )
        return summary

    def _resolve_log_paths(self, log_row: CrashLog) -> tuple[Any, Any, str | None]:
        if not log_row.zst_path or not log_row.idx_path:
            return (
                None,
                None,
                "Log file is no longer available on disk (evicted by retention GC)",
            )
        try:
            abs_zst = self.storage.resolve_path(log_row.zst_path)
            abs_idx = self.storage.resolve_path(log_row.idx_path)
        except StorageError as exc:
            return None, None, str(exc)

        if not abs_zst.exists() or not abs_idx.exists():
            return (
                None,
                None,
                "Log file is no longer available on disk (evicted by retention GC)",
            )
        return abs_zst, abs_idx, None

    async def _tool_read_log_lines(
        self,
        session: AsyncSession,
        crash_report: CrashReport,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        log_row, err = await self._load_crash_log(
            session, crash_report.id, args.get("log_id")
        )
        if log_row is None:
            return {"error": err or "Log not found"}

        abs_zst, abs_idx, path_err = self._resolve_log_paths(log_row)
        if path_err is not None:
            return {"error": path_err}

        try:
            start = max(1, int(args.get("start_line", 1)))
            req_end = max(start, int(args.get("end_line", start)))
        except (TypeError, ValueError):
            return {"error": "start_line and end_line must be integers"}

        clamped_end = min(req_end, start + 199)
        capped = req_end > clamped_end

        lines = await asyncio.to_thread(
            self.log_engine.read_lines,
            abs_zst,
            abs_idx,
            start,
            clamped_end,
        )
        return {
            "log_id": str(log_row.id),
            "total_lines": log_row.total_lines,
            "start_line": start,
            "end_line": clamped_end,
            "capped_to_200": capped,
            "lines": lines,
        }

    @staticmethod
    def _resolve_tool_session_filter(
        crash_report: CrashReport,
        args: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        raw_sid = args.get("session_id")
        if raw_sid is not None:
            if not isinstance(raw_sid, str):
                return None, "session_id must be a string"
            try:
                cleaned = normalize_session_id(raw_sid)
            except ValueError as exc:
                return None, str(exc)
            if cleaned is not None:
                return cleaned, None
        if bool(args.get("only_session", False)):
            if crash_report.session_id:
                return crash_report.session_id, None
            return None, "only_session=true requested, but this crash has no session_id"
        return None, None

    async def _tool_read_log_around_time(
        self,
        session: AsyncSession,
        crash_report: CrashReport,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        log_row, err = await self._load_crash_log(
            session, crash_report.id, args.get("log_id")
        )
        if log_row is None:
            return {"error": err or "Log not found"}

        abs_zst, abs_idx, path_err = self._resolve_log_paths(log_row)
        if path_err is not None:
            return {"error": path_err}

        session_filter, session_err = self._resolve_tool_session_filter(crash_report, args)
        if session_err is not None:
            return {"error": session_err}

        explicit_ts = args.get("timestamp")
        target_ts: Any = explicit_ts or _as_utc(crash_report.crash_time)
        try:
            window_sec = max(1, min(120, int(args.get("window_seconds", 10))))
            max_lines = max(1, min(200, int(args.get("max_lines", 200))))
        except (TypeError, ValueError):
            return {"error": "window_seconds and max_lines must be integers"}

        try:
            lines = await asyncio.to_thread(
                self.log_engine.read_around_time,
                abs_zst,
                abs_idx,
                target_ts,
                window_sec,
                max_lines,
                session_filter,
            )
        except ValueError as exc:
            return {"error": str(exc)}

        anchored_to: str | None = None
        if not lines and not explicit_ts and session_filter is not None:
            sess_digest = (log_row.digest_json or {}).get("session")
            if (
                isinstance(sess_digest, dict)
                and isinstance(sess_digest.get("last_ts"), str)
                and sess_digest["last_ts"].strip()
                and str(sess_digest.get("session_id") or "").lower() == session_filter.lower()
            ):
                fallback_ts = sess_digest["last_ts"].strip()
                try:
                    fallback_lines = await asyncio.to_thread(
                        self.log_engine.read_around_time,
                        abs_zst,
                        abs_idx,
                        fallback_ts,
                        window_sec,
                        max_lines,
                        session_filter,
                    )
                    if fallback_lines:
                        lines = fallback_lines
                        target_ts = fallback_ts
                        anchored_to = "session_last_ts"
                except ValueError:
                    pass

        resp: dict[str, Any] = {
            "log_id": str(log_row.id),
            "target_timestamp": (
                target_ts.isoformat() if isinstance(target_ts, datetime) else str(target_ts)
            ),
            "window_seconds": window_sec,
            "session_id": session_filter,
            "lines": lines,
        }
        if anchored_to is not None:
            resp["anchored_to"] = anchored_to
        return resp

    async def _tool_search_log(
        self,
        session: AsyncSession,
        crash_report: CrashReport,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        log_row, err = await self._load_crash_log(
            session, crash_report.id, args.get("log_id")
        )
        if log_row is None:
            return {"error": err or "Log not found"}

        abs_zst, abs_idx, path_err = self._resolve_log_paths(log_row)
        if path_err is not None:
            return {"error": path_err}

        session_filter, session_err = self._resolve_tool_session_filter(crash_report, args)
        if session_err is not None:
            return {"error": session_err}

        pattern = str(args.get("pattern") or "")
        is_regex = bool(args.get("is_regex", False))
        case_sensitive = bool(args.get("case_sensitive", False))
        try:
            max_matches = max(1, min(100, int(args.get("max_matches", 50))))
        except (TypeError, ValueError):
            return {"error": "max_matches must be an integer"}

        try:
            res = await asyncio.to_thread(
                self.log_engine.search,
                abs_zst,
                abs_idx,
                pattern,
                is_regex,
                max_matches,
                case_sensitive,
                session_filter,
            )
        except ValueError as exc:
            return {"error": str(exc)}

        return {
            "log_id": str(log_row.id),
            "pattern": pattern,
            "session_id": session_filter,
            "total_matches": res["total_matches"],
            "truncated": res["truncated"],
            "elapsed_ms": res["elapsed_ms"],
            "matches": res["matches"],
        }

    async def _tool_search_known_issues(
        self,
        session: AsyncSession,
        crash_report: CrashReport,
        project: Project | None,
        runtime_cfg: dict[str, Any],
        args: dict[str, Any],
    ) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        module = str(args.get("module") or "").strip()
        function = str(args.get("function") or "").strip()

        noise_rules = self.clustering_service.resolve_noise_rules(
            project.settings_json if project else None,
            runtime_cfg,
        )
        frames = self.clustering_service.filter_and_normalize_frames(
            crash_report.parsed_stacks_json,
            noise_rules,
            crash_reason=crash_report.crash_reason,
        )
        if module or function:
            norm_fn = self.clustering_service.normalize_function_name(function) or "unknown"
            mod_name = (module.replace("\\", "/").rsplit("/", 1)[-1].lower()) or "unknown"
            frames = [
                {
                    "module": mod_name,
                    "function": norm_fn,
                    "normalized": f"{mod_name}!{norm_fn}",
                },
                *frames,
            ]

        query_parts = [part for part in (query, module, function) if part]
        query_combined = " ".join(query_parts)

        recalled = await self.clustering_service.recall_similar_issues(
            session,
            crash_report.project_id,
            frames[:10],
            query_text=query_combined,
            limit=10,
        )

        merged: dict[int, dict[str, Any]] = {}
        for item in recalled:
            merged[item["issue_id"]] = {
                "issue_id": item["issue_id"],
                "title": item["title"],
                "status": item["status"],
                "component": item["component"],
                "fixed_in_version": item["fixed_in_version"],
                "is_ambiguous_stack": item["is_ambiguous_stack"],
                "crash_count": item["crash_count"],
                "score": item["score"],
                "match_reason": "stack_and_trigram_recall",
                "root_cause_preview": (item.get("root_cause") or "")[:300],
                "log_patterns": item.get("log_patterns") or [],
            }

        # Supplement with direct SQL query (supports query=="" returning recent project issues).
        stmt = select(Issue).where(Issue.project_id == crash_report.project_id)
        if query_parts:
            filters = []
            for token in query_parts:
                like_pat = f"%{token}%"
                filters.append(Issue.title.ilike(like_pat))
                filters.append(Issue.root_cause.ilike(like_pat))
                filters.append(Issue.component.ilike(like_pat))
            stmt = stmt.where(or_(*filters))
        stmt = stmt.order_by(Issue.crash_count.desc(), Issue.last_seen_at.desc()).limit(10)

        sql_rows = (await session.execute(stmt)).scalars().all()
        keyword_score = 0.90 if query_parts else 0.0
        for issue in sql_rows:
            if issue.id in merged:
                if query_parts:
                    merged[issue.id]["score"] = round(max(merged[issue.id]["score"], 0.95), 6)
                    merged[issue.id]["match_reason"] = "stack_and_keyword_match"
            else:
                merged[issue.id] = {
                    "issue_id": issue.id,
                    "title": issue.title,
                    "status": issue.status,
                    "component": issue.component,
                    "fixed_in_version": issue.fixed_in_version,
                    "is_ambiguous_stack": issue.is_ambiguous_stack,
                    "crash_count": issue.crash_count,
                    "score": keyword_score,
                    "match_reason": "project_issue_catalog" if not query_parts else "keyword_match",
                    "root_cause_preview": (issue.root_cause or "")[:300],
                    "log_patterns": list(issue.log_patterns_json or []),
                }

        items = list(merged.values())
        items.sort(key=lambda x: (x["score"], x["crash_count"]), reverse=True)
        return {"count": len(items), "issues": items[:10]}

    async def _tool_get_issue_detail(
        self,
        session: AsyncSession,
        crash_report: CrashReport,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            issue_id = int(args.get("issue_id"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return {"error": "Invalid issue_id"}

        issue = (
            await session.execute(
                select(Issue).where(
                    Issue.id == issue_id,
                    Issue.project_id == crash_report.project_id,
                )
            )
        ).scalar_one_or_none()
        if issue is None:
            return {"error": "Issue not found"}

        clusters = (
            await session.execute(
                select(CrashCluster).where(
                    CrashCluster.issue_id == issue.id,
                    CrashCluster.project_id == crash_report.project_id,
                )
            )
        ).scalars().all()

        return {
            "issue_id": issue.id,
            "title": issue.title,
            "status": issue.status,
            "component": issue.component,
            "root_cause": issue.root_cause,
            "log_patterns_json": list(issue.log_patterns_json or []),
            "fixed_in_version": issue.fixed_in_version,
            "is_ambiguous_stack": issue.is_ambiguous_stack,
            "crash_count": issue.crash_count,
            "first_seen_at": _as_utc(issue.first_seen_at).isoformat(),
            "last_seen_at": _as_utc(issue.last_seen_at).isoformat(),
            "clusters": [
                {
                    "id": str(c.id),
                    "signature_hash": c.signature_hash,
                    "signature_text": c.signature_text,
                    "exception_type": c.exception_type,
                    "signature_frames_json": list(c.signature_frames_json or [])[:10],
                    "crash_count": c.crash_count,
                }
                for c in clusters
            ],
        }

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
        await session.flush()
        await session.refresh(crash_report, with_for_update=True)

        verdict = str(result_dict.get("verdict") or "NEEDS_REVIEW")
        raw_confidence = result_dict.get("confidence")
        confidence: float | None = float(raw_confidence) if raw_confidence is not None else None
        matched_issue_id = result_dict.get("matched_issue_id")
        key_patterns = list(result_dict.get("key_log_patterns") or [])

        old_issue_id = crash_report.issue_id
        crash_ts = _as_utc(crash_report.crash_time)
        cluster = (
            await session.get(CrashCluster, crash_report.cluster_id)
            if crash_report.cluster_id is not None
            else None
        )
        if cluster is not None:
            await session.refresh(cluster)

        async def _transition_crash_issue(target_id: int, previous_id: int | None) -> bool:
            if previous_id == target_id:
                await session.refresh(crash_report, attribute_names=["issue_id"])
                return False
            cond_old = (
                CrashReport.issue_id.is_(None)
                if previous_id is None
                else CrashReport.issue_id == previous_id
            )
            upd_res = await session.execute(
                update(CrashReport)
                .where(
                    CrashReport.id == crash_report.id,
                    cond_old,
                    CrashReport.is_human_verified.is_(False),
                )
                .values(issue_id=target_id)
            )
            if (upd_res.rowcount or 0) <= 0:
                await session.refresh(crash_report, attribute_names=["issue_id"])
                return False
            if previous_id is not None:
                await session.execute(
                    update(Issue)
                    .where(Issue.id == previous_id)
                    .values(
                        crash_count=case(
                            (Issue.crash_count > 0, Issue.crash_count - 1),
                            else_=0,
                        )
                    )
                )
                old_issue = await session.get(Issue, previous_id)
                if old_issue is not None:
                    await session.refresh(old_issue, attribute_names=["crash_count"])
            await session.execute(
                update(Issue)
                .where(Issue.id == target_id)
                .values(crash_count=Issue.crash_count + 1)
            )
            await session.refresh(crash_report, attribute_names=["issue_id"])
            return True

        async def _assign_cluster_issue_if_empty(target_issue_id: int) -> int | None:
            if cluster is None:
                return None
            await session.execute(
                update(CrashCluster)
                .where(CrashCluster.id == cluster.id, CrashCluster.issue_id.is_(None))
                .values(issue_id=target_issue_id)
            )
            await session.refresh(cluster, attribute_names=["issue_id"])
            return cluster.issue_id

        should_write_verdict = True
        matched_issue: Issue | None = None
        if crash_report.is_human_verified:
            result_dict["matched_issue_id"] = crash_report.issue_id
            should_write_verdict = False
        elif verdict in {"KNOWN_ISSUE", "REGRESSION"}:
            if (
                matched_issue_id is not None
                and not isinstance(matched_issue_id, bool)
                and 1 <= int(matched_issue_id) <= _INT32_MAX
            ):
                matched_issue = (
                    await session.execute(
                        select(Issue).where(
                            Issue.id == int(matched_issue_id),
                            Issue.project_id == crash_report.project_id,
                        )
                    )
                ).scalar_one_or_none()
            if matched_issue is None:
                verdict = "NEEDS_REVIEW"
                result_dict["verdict"] = "NEEDS_REVIEW"
                result_dict["matched_issue_id"] = None
            else:
                await _transition_crash_issue(matched_issue.id, old_issue_id)
                await session.refresh(crash_report)
                won_or_already_owned = crash_report.issue_id == matched_issue.id
                if won_or_already_owned:
                    await session.refresh(matched_issue)
                    if matched_issue.status in {"FIXED", "REGRESSION"}:
                        if matched_issue.fixed_in_version:
                            if (
                                _compare_versions(
                                    crash_report.app_version,
                                    matched_issue.fixed_in_version,
                                )
                                >= 0
                            ):
                                verdict = "REGRESSION"
                                result_dict["verdict"] = "REGRESSION"
                                if matched_issue.status == "FIXED":
                                    matched_issue.status = "REGRESSION"
                            else:
                                if verdict == "REGRESSION":
                                    verdict = "KNOWN_ISSUE"
                                    result_dict["verdict"] = "KNOWN_ISSUE"
                        elif verdict == "REGRESSION" and matched_issue.status == "FIXED":
                            matched_issue.status = "REGRESSION"
                    elif verdict == "REGRESSION":
                        verdict = "KNOWN_ISSUE"
                        result_dict["verdict"] = "KNOWN_ISSUE"

                    if _as_utc(matched_issue.last_seen_at) < crash_ts:
                        matched_issue.last_seen_at = crash_ts

                    if key_patterns:
                        existing_patterns = list(matched_issue.log_patterns_json or [])
                        matched_issue.log_patterns_json = list(
                            dict.fromkeys([*existing_patterns, *key_patterns])
                        )

                    if not matched_issue.is_ambiguous_stack:
                        await _assign_cluster_issue_if_empty(matched_issue.id)
                elif crash_report.issue_id is not None:
                    should_write_verdict = False
                    result_dict["matched_issue_id"] = crash_report.issue_id

        elif verdict == "NEW_ISSUE":
            reused_cluster_issue = False
            if cluster is not None and cluster.issue_id is not None:
                existing_issue = await session.get(Issue, cluster.issue_id)
                if (
                    existing_issue is not None
                    and existing_issue.project_id == crash_report.project_id
                ):
                    await _transition_crash_issue(existing_issue.id, old_issue_id)
                    await session.refresh(crash_report)
                    if crash_report.issue_id == existing_issue.id:
                        await session.refresh(existing_issue)
                        if _as_utc(existing_issue.last_seen_at) < crash_ts:
                            existing_issue.last_seen_at = crash_ts
                        result_dict["matched_issue_id"] = existing_issue.id
                    elif crash_report.issue_id is not None:
                        should_write_verdict = False
                        result_dict["matched_issue_id"] = crash_report.issue_id
                    reused_cluster_issue = True
                else:
                    cluster.issue_id = None

            if not reused_cluster_issue:
                title = (result_dict.get("suggested_title") or "Untitled Crash Issue").strip()[:512]
                raw_comp = result_dict.get("suggested_component")
                comp_val = (
                    str(raw_comp).strip()[:128]
                    if raw_comp is not None and str(raw_comp).strip()
                    else None
                )
                new_issue = Issue(
                    project_id=crash_report.project_id,
                    title=title or "Untitled Crash Issue",
                    status="NEW",
                    component=comp_val,
                    root_cause=result_dict.get("root_cause_analysis") or None,
                    log_patterns_json=list(dict.fromkeys(key_patterns)),
                    is_ambiguous_stack=False,
                    is_pinned=False,
                    crash_count=0,
                    first_seen_at=crash_ts,
                    last_seen_at=crash_ts,
                )
                session.add(new_issue)
                await session.flush()
                transitioned = await _transition_crash_issue(new_issue.id, old_issue_id)
                await session.refresh(crash_report)
                if transitioned:
                    final_issue_id = new_issue.id
                    assigned_issue_id = await _assign_cluster_issue_if_empty(new_issue.id)
                    if assigned_issue_id is not None and assigned_issue_id != new_issue.id:
                        winner = await session.get(Issue, assigned_issue_id)
                        if winner is not None and winner.project_id == crash_report.project_id:
                            if await _transition_crash_issue(winner.id, new_issue.id):
                                final_issue_id = winner.id
                                if _as_utc(winner.last_seen_at) < crash_ts:
                                    winner.last_seen_at = crash_ts
                                await session.delete(new_issue)
                    result_dict["matched_issue_id"] = final_issue_id
                elif crash_report.issue_id is not None:
                    await session.delete(new_issue)
                    should_write_verdict = False
                    result_dict["matched_issue_id"] = crash_report.issue_id
                else:
                    await session.refresh(new_issue, attribute_names=["crash_count"])
                    result_dict["matched_issue_id"] = new_issue.id

        if should_write_verdict:
            crash_report.ai_verdict = verdict
            crash_report.ai_confidence = confidence
        if crash_report.status in {"PENDING", "PARSING", "TRIAGING"} or should_write_verdict:
            crash_report.status = "ANALYZED"

        run = AIAnalysisRun(
            crash_id=crash_report.id,
            model_name=model_name,
            status=run_status,
            result_json=result_dict,
            trace_json=trace,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=duration_ms,
            created_at=utcnow(),
        )
        session.add(run)
        await session.flush()
        if commit:
            await session.commit()
            await session.refresh(run)
            await session.refresh(crash_report)
        return run


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
