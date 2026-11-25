"""Read-only investigation tools exposed to the AI triage agent."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CrashCluster, CrashLog, CrashReport, Issue, Project
from app.services.ai_agent.output_trimming import (
    _as_utc,
    _clip_digest_entries,
    _compact_overview_frame,
)
from app.services.clustering import ClusteringService
from app.services.log_engine import LogEngine, normalize_session_id
from app.services.storage import StorageError, StorageManager

logger = logging.getLogger(__name__)


class AgentToolsMixin:
    """Mixin implementing the 8 read-only crash, log, and KB investigation tools."""

    storage: StorageManager
    log_engine: LogEngine
    clustering_service: ClusteringService

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
