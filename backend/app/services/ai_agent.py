"""OpenAI-compatible ReAct AI triage agent with 8 read-only tools and strict guardrails."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import openai
from sqlalchemy import case, or_, select, update
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
from app.services import settings_service
from app.services.clustering import ClusteringService, _compare_versions
from app.services.log_engine import LogEngine
from app.services.storage import StorageError, StorageManager

logger = logging.getLogger(__name__)

VALID_VERDICTS: frozenset[str] = frozenset(
    {"KNOWN_ISSUE", "NEW_ISSUE", "REGRESSION", "NEEDS_REVIEW"}
)
TRUNCATION_MARKER = "\n...[TRUNCATED]"
_INT32_MAX = 2_147_483_647

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)

AI_SYSTEM_PROMPT = """You are a senior C++ Windows/Native Crash Triage Engineer for CrashKiller.
You investigate a crash dump and its diagnostic logs using ONLY the provided read-only tools.

Investigation rules:
1. Start by calling get_crash_overview to inspect crashing stack frames, missing symbols,
   candidate known issues, and attached log IDs.
2. Inspect attached logs (get_log_summary, search_log, read_log_around_time, read_log_lines),
   other threads (get_other_threads), or known issues (search_known_issues, get_issue_detail).
3. Never invent log line numbers, file contents, or issue_id values. Cite only observed tool data.
4. When finished, output ONLY a single valid JSON object (no surrounding prose):
{
  "verdict": "KNOWN_ISSUE | NEW_ISSUE | REGRESSION | NEEDS_REVIEW",
  "matched_issue_id": 42,
  "confidence": 0.91,
  "suggested_title": "concise issue title",
  "root_cause_analysis": "technical explanation grounded in stack + logs",
  "reasoning_summary": "step-by-step justification",
  "evidence_log_snippets": [
    {"log_id": "uuid", "start_line": 1, "end_line": 5, "relevance": "why this matters"}
  ],
  "key_log_patterns": ["distinctive error substring"],
  "suggested_component": "subsystem name or null"
}
"""

AI_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_crash_overview",
            "description": (
                "Get crash metadata, crashing thread top frames, missing symbols, "
                "cluster info, fuzzy-recalled candidate issues, and attached log metadata."
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
                "Get precomputed log digest (error/warn counts, tail lines, recent errors)."
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
                "(defaults to crash_time)."
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
            "description": "Search an attached compressed log for a substring or regex pattern.",
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


def _parse_verdict_json(raw_content: str | None) -> dict[str, Any]:
    """Parse and validate strict JSON verdict output from the LLM."""
    if not raw_content or not str(raw_content).strip():
        raise ValueError("Empty LLM response content")

    cleaned = _strip_markdown_fences(str(raw_content))
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("LLM verdict output must be a JSON object")

    verdict = str(data.get("verdict") or "").strip().upper()
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"Invalid verdict: {data.get('verdict')!r}")

    raw_matched_id = data.get("matched_issue_id")
    matched_issue_id: int | None
    if raw_matched_id is None or raw_matched_id == "":
        matched_issue_id = None
    elif isinstance(raw_matched_id, bool):
        raise ValueError("matched_issue_id cannot be boolean")
    else:
        parsed_id = int(raw_matched_id)
        matched_issue_id = parsed_id if 1 <= parsed_id <= _INT32_MAX else None

    raw_conf = data.get("confidence", 0.5)
    if isinstance(raw_conf, bool):
        raise ValueError("confidence cannot be boolean")
    try:
        confidence = float(raw_conf)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid confidence value: {raw_conf!r}") from exc
    if not math.isfinite(confidence):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

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
    suggested_component = (
        str(comp).strip()[:128] if comp is not None and str(comp).strip() else None
    )

    return {
        "verdict": verdict,
        "matched_issue_id": matched_issue_id,
        "confidence": round(confidence, 4),
        "suggested_title": str(data.get("suggested_title") or "").strip()[:512],
        "root_cause_analysis": str(data.get("root_cause_analysis") or "").strip(),
        "reasoning_summary": str(data.get("reasoning_summary") or "").strip(),
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
            "llm_base_url": effective.llm_base_url,
            "llm_api_key": effective.llm_api_key,
            "llm_model": effective.llm_model,
            "ai_triage_mode": effective.ai_triage_mode,
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
            top_frames = list((threads[crashing_idx].get("frames") or [])[:30])
        elif crash_report.parsed_summary_json:
            top_frames = list((crash_report.parsed_summary_json.get("top_frames") or [])[:30])

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
        logs_list = [
            {
                "id": str(log.id),
                "filename": log.filename,
                "raw_size": log.raw_size,
                "total_lines": log.total_lines,
                "error_count": int((log.digest_json or {}).get("error_count", 0)),
                "warn_count": int((log.digest_json or {}).get("warn_count", 0)),
            }
            for log in logs_rows
        ]

        return {
            "crash_id": str(crash_report.id),
            "project_id": str(crash_report.project_id),
            "app_version": crash_report.app_version,
            "build_id": crash_report.build_id,
            "os_version": crash_report.os_version,
            "crash_reason": crash_report.crash_reason,
            "crash_address": crash_report.crash_address,
            "crash_time": _as_utc(crash_report.crash_time).isoformat(),
            "crashing_thread_index": crashing_idx,
            "top_frames": top_frames,
            "missing_symbols": list(crash_report.missing_symbols_json or []),
            "cluster": cluster_info,
            "candidate_issues": candidate_issues,
            "logs": logs_list,
        }

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
        return {
            "log_id": str(log_row.id),
            "filename": log_row.filename,
            "total_lines": log_row.total_lines,
            "raw_size": log_row.raw_size,
            "compressed_size": log_row.compressed_size,
            "error_count": int(digest.get("error_count", 0)),
            "warn_count": int(digest.get("warn_count", 0)),
            "tail_lines": list(digest.get("tail_lines") or [])[-80:],
            "recent_errors": list(digest.get("recent_errors") or [])[-50:],
        }

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

        target_ts = args.get("timestamp") or _as_utc(crash_report.crash_time)
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
            )
        except ValueError as exc:
            return {"error": str(exc)}

        return {
            "log_id": str(log_row.id),
            "target_timestamp": (
                target_ts.isoformat() if isinstance(target_ts, datetime) else str(target_ts)
            ),
            "window_seconds": window_sec,
            "lines": lines,
        }

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
            )
        except ValueError as exc:
            return {"error": str(exc)}

        return {
            "log_id": str(log_row.id),
            "pattern": pattern,
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
    ) -> Any:
        try:
            return await asyncio.wait_for(
                client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    tools=AI_TOOL_DEFINITIONS,
                    tool_choice="auto",
                    temperature=0.1,
                    response_format={"type": "json_object"},
                ),
                timeout=timeout_budget,
            )
        except openai.BadRequestError:
            # Fallback for OpenAI-compatible endpoints that disallow response_format with tools.
            return await asyncio.wait_for(
                client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    tools=AI_TOOL_DEFINITIONS,
                    tool_choice="auto",
                    temperature=0.1,
                ),
                timeout=timeout_budget,
            )

    async def run_triage(
        self,
        session: AsyncSession,
        crash_report: CrashReport,
        settings_override: dict[str, Any] | None = None,
        openai_client: Any | None = None,
        *,
        commit: bool = True,
    ) -> AIAnalysisRun:
        """Execute the ReAct tool-calling triage loop and persist verdict + trace."""
        start_mono = time.monotonic()
        runtime_cfg = await self._resolve_runtime_config(session, settings_override)

        model_name = str(runtime_cfg["llm_model"])
        max_steps = int(runtime_cfg["ai_max_steps"])
        timeout_seconds = int(runtime_cfg["ai_timeout_seconds"])
        tool_max_chars = int(runtime_cfg["ai_tool_output_max_chars"])
        api_key = str(runtime_cfg["llm_api_key"] or "").strip()
        base_url = str(runtime_cfg["llm_base_url"])

        if openai_client is None and not api_key:
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
        owns_client = openai_client is None
        client = (
            openai_client
            if openai_client is not None
            else openai.AsyncOpenAI(
                api_key=api_key,
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

        user_prompt = (
            f"Triage CrashReport {crash_report.id} "
            f"(app_version={crash_report.app_version}, "
            f"crash_reason={crash_report.crash_reason or 'UNKNOWN'}). "
            "Use your tools to investigate stack traces, logs, and known issues, "
            "then respond with ONLY the final JSON verdict."
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        deadline = start_mono + float(timeout_seconds)

        async def _run_loop() -> None:
            nonlocal prompt_tokens, completion_tokens, run_status, result_dict
            for _step_idx in range(1, max_steps + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("AI triage exceeded AI_TIMEOUT_SECONDS budget")

                resp = await self._call_completion(client, model_name, messages, remaining)
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
                    completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)

                choices = getattr(resp, "choices", None) or []
                if not choices:
                    raise ValueError("LLM response contained no choices")
                msg = choices[0].message
                msg_content = getattr(msg, "content", None)
                tool_calls = getattr(msg, "tool_calls", None)

                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": msg_content,
                }
                if tool_calls:
                    assistant_msg["tool_calls"] = _serialize_tool_calls_for_message(tool_calls)
                messages.append(assistant_msg)

                if tool_calls:
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
                        truncated_out = _truncate_tool_output(raw_out, tool_max_chars)
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

                # Final non-tool message: parse strict JSON verdict.
                try:
                    parsed_verdict = _parse_verdict_json(msg_content)
                except Exception as exc:
                    err_reason = f"Malformed LLM JSON verdict: {exc}"
                    trace.append(
                        {
                            "step": len(trace) + 1,
                            "role": "system",
                            "error": err_reason,
                            "output_preview": str(msg_content or "")[:500],
                        }
                    )
                    result_dict = _build_needs_review_result(err_reason)
                    run_status = "FAILED"
                    return

                result_dict = parsed_verdict
                run_status = "COMPLETED"
                return

            # Exhausted max_steps without a final verdict.
            limit_reason = f"Exceeded AI_MAX_STEPS ({max_steps}) without final verdict"
            trace.append(
                {
                    "step": len(trace) + 1,
                    "role": "system",
                    "error": limit_reason,
                }
            )
            result_dict = _build_needs_review_result(limit_reason)
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
        confidence = float(result_dict.get("confidence") or 0.0)
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
) -> AIAnalysisRun:
    """Convenience wrapper executing AITriageAgent.run_triage for workers and callers."""
    agent = AITriageAgent(storage=storage, settings=settings)
    return await agent.run_triage(
        session=session,
        crash_report=crash_report,
        settings_override=settings_override,
        openai_client=openai_client,
    )
