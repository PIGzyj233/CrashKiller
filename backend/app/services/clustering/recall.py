"""Trigram/Jaccard similarity scoring and fuzzy known-issue recall."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CrashCluster, Issue
from app.services.clustering.normalization import _frame_key
from app.services.clustering.types import _as_utc

_TRGM_TOKEN_RE = re.compile(r"[0-9a-zA-Z]+")


def _trigram_set(text: str) -> set[str]:
    if not text:
        return set()
    grams: set[str] = set()
    for token in _TRGM_TOKEN_RE.findall(text.lower()):
        padded = f"  {token} "
        for idx in range(len(padded) - 2):
            grams.add(padded[idx : idx + 3])
    return grams


def _trigram_similarity(a: str, b: str) -> float:
    """Pure-Python approximation of PostgreSQL pg_trgm word-padded trigram similarity."""
    ta = _trigram_set(a)
    tb = _trigram_set(b)
    if not ta or not tb:
        return 0.0
    union = ta | tb
    if not union:
        return 0.0
    return len(ta & tb) / len(union)


def _weighted_top10_jaccard(
    frames_a: list[Any],
    frames_b: list[Any],
) -> float:
    """Position-weighted Jaccard over top 10 normalized frames (top frames weigh more)."""
    if not frames_a or not frames_b:
        return 0.0

    weights_a: dict[str, float] = {}
    for idx, frame in enumerate(frames_a[:10]):
        key = _frame_key(frame)
        w = 1.0 / (1.0 + 0.25 * idx)
        if w > weights_a.get(key, 0.0):
            weights_a[key] = w

    weights_b: dict[str, float] = {}
    for idx, frame in enumerate(frames_b[:10]):
        key = _frame_key(frame)
        w = 1.0 / (1.0 + 0.25 * idx)
        if w > weights_b.get(key, 0.0):
            weights_b[key] = w

    all_keys = weights_a.keys() | weights_b.keys()
    if not all_keys:
        return 0.0
    numerator = sum(min(weights_a.get(k, 0.0), weights_b.get(k, 0.0)) for k in all_keys)
    denominator = sum(max(weights_a.get(k, 0.0), weights_b.get(k, 0.0)) for k in all_keys)
    if denominator <= 0.0:
        return 0.0
    return numerator / denominator


async def recall_similar_issues(
    session: AsyncSession,
    project_id: uuid.UUID,
    normalized_frames: list[dict[str, Any]],
    query_text: str = "",
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Recall top-k similar known issues using weighted Top-10 Jaccard + pg_trgm/trigram."""
    if limit <= 0:
        return []

    top10_frames = normalized_frames[:10]
    stack_query_text = " ".join(_frame_key(f) for f in top10_frames).strip()
    clean_query = (query_text or "").strip()
    trgm_query = f"{clean_query} {stack_query_text}".strip()

    bind = session.get_bind()
    is_pg = bind is not None and bind.dialect.name == "postgresql"

    issues_by_id: dict[int, Issue] = {}
    clusters_by_issue: dict[int, list[CrashCluster]] = {}
    cluster_trgm_scores: dict[uuid.UUID, float] = {}
    issue_pg_trgm_scores: dict[int, float] = {}

    if is_pg:
        if stack_query_text:
            sim_col = func.similarity(CrashCluster.signature_text, stack_query_text).label("sim")
            cluster_stmt = (
                select(CrashCluster, Issue, sim_col)
                .join(Issue, CrashCluster.issue_id == Issue.id)
                .where(
                    CrashCluster.project_id == project_id,
                    CrashCluster.issue_id.is_not(None),
                )
                .order_by(sim_col.desc(), Issue.crash_count.desc())
                .limit(max(limit * 6, 30))
            )
            for cluster, issue, sim in (await session.execute(cluster_stmt)).all():
                issues_by_id[issue.id] = issue
                clusters_by_issue.setdefault(issue.id, []).append(cluster)
                cluster_trgm_scores[cluster.id] = float(sim or 0.0)
        else:
            cluster_stmt = (
                select(CrashCluster, Issue)
                .join(Issue, CrashCluster.issue_id == Issue.id)
                .where(
                    CrashCluster.project_id == project_id,
                    CrashCluster.issue_id.is_not(None),
                )
                .order_by(Issue.crash_count.desc(), Issue.last_seen_at.desc())
                .limit(max(limit * 6, 30))
            )
            for cluster, issue in (await session.execute(cluster_stmt)).all():
                issues_by_id[issue.id] = issue
                clusters_by_issue.setdefault(issue.id, []).append(cluster)

        if trgm_query:
            issue_sim = func.greatest(
                func.similarity(Issue.title, trgm_query),
                func.similarity(func.coalesce(Issue.root_cause, ""), trgm_query),
            ).label("issue_sim")
            issue_stmt = (
                select(Issue, issue_sim)
                .where(Issue.project_id == project_id)
                .order_by(issue_sim.desc(), Issue.crash_count.desc())
                .limit(max(limit * 4, 20))
            )
            for issue, sim in (await session.execute(issue_stmt)).all():
                issues_by_id[issue.id] = issue
                issue_pg_trgm_scores[issue.id] = float(sim or 0.0)
        else:
            issue_stmt = (
                select(Issue)
                .where(Issue.project_id == project_id)
                .order_by(Issue.crash_count.desc(), Issue.last_seen_at.desc())
                .limit(max(limit * 4, 20))
            )
            for issue in (await session.execute(issue_stmt)).scalars().all():
                issues_by_id[issue.id] = issue
    else:
        issue_rows = (
            await session.execute(select(Issue).where(Issue.project_id == project_id))
        ).scalars().all()
        for issue in issue_rows:
            issues_by_id[issue.id] = issue

        cluster_rows = (
            await session.execute(
                select(CrashCluster).where(
                    CrashCluster.project_id == project_id,
                    CrashCluster.issue_id.is_not(None),
                )
            )
        ).scalars().all()
        for cluster in cluster_rows:
            if cluster.issue_id is not None and cluster.issue_id in issues_by_id:
                clusters_by_issue.setdefault(cluster.issue_id, []).append(cluster)
                cluster_trgm_scores[cluster.id] = (
                    _trigram_similarity(stack_query_text, cluster.signature_text or "")
                    if stack_query_text
                    else 0.0
                )

    if not issues_by_id:
        return []

    scored: list[tuple[float, int, datetime, dict[str, Any]]] = []
    q_lower = clean_query.lower()

    for issue_id, issue in issues_by_id.items():
        linked_clusters = clusters_by_issue.get(issue_id, [])

        best_jaccard = 0.0
        best_cluster_trgm = 0.0
        best_cluster_id: str | None = None
        best_cluster_signal = -1.0

        for cluster in linked_clusters:
            jacc = _weighted_top10_jaccard(
                top10_frames,
                cluster.signature_frames_json or [],
            )
            ctrgm = cluster_trgm_scores.get(cluster.id, 0.0)
            if jacc > best_jaccard:
                best_jaccard = jacc
            if ctrgm > best_cluster_trgm:
                best_cluster_trgm = ctrgm
            signal = 0.7 * jacc + 0.3 * ctrgm
            if signal > best_cluster_signal:
                best_cluster_signal = signal
                best_cluster_id = str(cluster.id)

        if is_pg and issue_id in issue_pg_trgm_scores:
            issue_text_trgm = issue_pg_trgm_scores[issue_id]
        else:
            issue_doc = " ".join(
                [
                    issue.title or "",
                    issue.root_cause or "",
                    issue.component or "",
                    " ".join(issue.log_patterns_json or []),
                ]
            ).strip()
            issue_text_trgm = _trigram_similarity(trgm_query, issue_doc) if trgm_query else 0.0

        trigram_score = max(best_cluster_trgm, issue_text_trgm)
        substring_boost = 0.0
        if q_lower:
            haystack = (
                f"{issue.title or ''} {issue.root_cause or ''} {issue.component or ''}"
            ).lower()
            if q_lower in haystack:
                substring_boost = 0.5

        if top10_frames:
            combined_score = (
                0.60 * best_jaccard
                + 0.25 * best_cluster_trgm
                + 0.15 * issue_text_trgm
                + (0.15 * substring_boost)
            )
        elif q_lower:
            combined_score = issue_text_trgm + substring_boost
        else:
            combined_score = 0.0

        entry = {
            "issue_id": issue.id,
            "title": issue.title,
            "status": issue.status,
            "component": issue.component,
            "root_cause": issue.root_cause,
            "fixed_in_version": issue.fixed_in_version,
            "is_ambiguous_stack": issue.is_ambiguous_stack,
            "crash_count": issue.crash_count,
            "log_patterns": list(issue.log_patterns_json or []),
            "score": round(float(combined_score), 6),
            "jaccard_score": round(float(best_jaccard), 6),
            "trigram_score": round(float(trigram_score), 6),
            "matched_cluster_id": best_cluster_id,
        }
        scored.append(
            (
                float(combined_score),
                int(issue.crash_count or 0),
                _as_utc(issue.last_seen_at),
                entry,
            )
        )

    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return [item[3] for item in scored[:limit]]
