"""Database-backed crash clustering and human verdict KB feedback operations."""

from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CrashCluster, CrashJob, CrashReport, Issue, Project
from app.services.clustering.normalization import (
    _coerce_noise_rules,
    _frame_key,
    compute_signature_hash,
    filter_and_normalize_frames,
    normalize_exception_category,
    resolve_noise_rules,
)
from app.services.clustering.recall import recall_similar_issues
from app.services.clustering.types import (
    VALID_ISSUE_STATUSES,
    ClusteringError,
    ClusterResult,
    NoiseRules,
    _as_utc,
)
from app.services.clustering.versions import _is_post_fix_version
from app.services.issue_workflow import record_issue_status_event


async def cluster_crash(
    session: AsyncSession,
    crash_report: CrashReport,
    project: Project,
    triage_mode: str | None = None,
    system_settings: dict[str, Any] | None = None,
    noise_rules: NoiseRules | dict[str, Any] | None = None,
) -> ClusterResult:
    """Cluster a parsed CrashReport, reconcile counts, and decide AI triage eligibility."""
    rules = (
        _coerce_noise_rules(noise_rules)
        if noise_rules is not None
        else resolve_noise_rules(project.settings_json, system_settings)
    )
    normalized_frames = filter_and_normalize_frames(
        crash_report.parsed_stacks_json,
        rules,
        crash_reason=crash_report.crash_reason,
    )
    top_10 = normalized_frames[:10]
    sig_hash = compute_signature_hash(project.id, crash_report.crash_reason, top_10)
    sig_text = " ".join(_frame_key(f) for f in top_10) or normalize_exception_category(
        crash_report.crash_reason
    )
    exc_cat = normalize_exception_category(crash_report.crash_reason)

    await session.flush()
    await session.refresh(
        crash_report,
        attribute_names=["cluster_id", "issue_id", "is_human_verified"],
        with_for_update=True,
    )
    old_cluster_id = crash_report.cluster_id
    old_issue_id = crash_report.issue_id
    human_verified = crash_report.is_human_verified

    existing_cluster = (
        await session.execute(
            select(CrashCluster).where(CrashCluster.signature_hash == sig_hash)
        )
    ).scalar_one_or_none()

    is_new_cluster = False
    if existing_cluster is None:
        try:
            async with session.begin_nested():
                cluster = CrashCluster(
                    project_id=project.id,
                    issue_id=None,
                    signature_hash=sig_hash,
                    signature_frames_json=top_10,
                    signature_text=sig_text,
                    exception_type=exc_cat,
                    crash_count=0,
                )
                session.add(cluster)
                await session.flush()
            is_new_cluster = True
        except IntegrityError:
            cluster = (
                await session.execute(
                    select(CrashCluster).where(CrashCluster.signature_hash == sig_hash)
                )
            ).scalar_one()
    else:
        cluster = existing_cluster
        if not cluster.signature_frames_json:
            cluster.signature_frames_json = top_10
        if not cluster.signature_text:
            cluster.signature_text = sig_text
        if not cluster.exception_type:
            cluster.exception_type = exc_cat

    # Reconcile cluster count atomically and idempotently.
    if old_cluster_id != cluster.id:
        cond_old_cluster = (
            CrashReport.cluster_id.is_(None)
            if old_cluster_id is None
            else CrashReport.cluster_id == old_cluster_id
        )
        cluster_upd = await session.execute(
            update(CrashReport)
            .where(CrashReport.id == crash_report.id, cond_old_cluster)
            .values(cluster_id=cluster.id)
        )
        if (cluster_upd.rowcount or 0) > 0:
            if old_cluster_id is not None:
                await session.execute(
                    update(CrashCluster)
                    .where(CrashCluster.id == old_cluster_id)
                    .values(
                        crash_count=case(
                            (CrashCluster.crash_count > 0, CrashCluster.crash_count - 1),
                            else_=0,
                        )
                    )
                )
                old_cluster = await session.get(CrashCluster, old_cluster_id)
                if old_cluster is not None:
                    await session.refresh(old_cluster, attribute_names=["crash_count"])
            await session.execute(
                update(CrashCluster)
                .where(CrashCluster.id == cluster.id)
                .values(crash_count=CrashCluster.crash_count + 1)
            )
            await session.refresh(cluster, attribute_names=["crash_count"])
        await session.refresh(crash_report, attribute_names=["cluster_id"])
        if old_cluster_id is not None and not human_verified:
            crash_report.ai_verdict = None
            crash_report.ai_confidence = None

    # Reconcile issue link & counts atomically, keeping CrashReport.issue_id synced for GC.
    linked_issue_id = old_issue_id if human_verified else cluster.issue_id
    linked_issue = (
        await session.get(Issue, linked_issue_id) if linked_issue_id is not None else None
    )
    if linked_issue is not None and not human_verified:
        if old_issue_id != linked_issue.id:
            cond_old_issue = (
                CrashReport.issue_id.is_(None)
                if old_issue_id is None
                else CrashReport.issue_id == old_issue_id
            )
            issue_upd = await session.execute(
                update(CrashReport)
                .where(CrashReport.id == crash_report.id, cond_old_issue)
                .values(issue_id=linked_issue.id)
            )
            if (issue_upd.rowcount or 0) > 0:
                if old_issue_id is not None:
                    await session.execute(
                        update(Issue)
                        .where(Issue.id == old_issue_id)
                        .values(
                            crash_count=case(
                                (Issue.crash_count > 0, Issue.crash_count - 1),
                                else_=0,
                            )
                        )
                    )
                    old_issue = await session.get(Issue, old_issue_id)
                    if old_issue is not None:
                        await session.refresh(old_issue, attribute_names=["crash_count"])
                await session.execute(
                    update(Issue)
                    .where(Issue.id == linked_issue.id)
                    .values(crash_count=Issue.crash_count + 1)
                )
                await session.refresh(linked_issue, attribute_names=["crash_count"])
            await session.refresh(crash_report, attribute_names=["issue_id"])
        crash_ts = _as_utc(crash_report.crash_time)
        if _as_utc(linked_issue.last_seen_at) < crash_ts:
            linked_issue.last_seen_at = crash_ts
    elif not human_verified:
        if old_issue_id is not None and old_cluster_id != cluster.id:
            issue_upd = await session.execute(
                update(CrashReport)
                .where(CrashReport.id == crash_report.id, CrashReport.issue_id == old_issue_id)
                .values(issue_id=None)
            )
            if (issue_upd.rowcount or 0) > 0:
                await session.execute(
                    update(Issue)
                    .where(Issue.id == old_issue_id)
                    .values(
                        crash_count=case(
                            (Issue.crash_count > 0, Issue.crash_count - 1),
                            else_=0,
                        )
                    )
                )
                old_issue = await session.get(Issue, old_issue_id)
                if old_issue is not None:
                    await session.refresh(old_issue)
            await session.refresh(crash_report, attribute_names=["issue_id"])

    raw_mode = (
        triage_mode
        or (project.settings_json or {}).get("ai_triage_mode")
        or (system_settings or {}).get("ai_triage_mode")
        or "SMART_TRIAGE"
    )
    effective_mode = str(raw_mode).strip().upper()
    if effective_mode not in {"SMART_TRIAGE", "ALWAYS", "MANUAL"}:
        raise ClusteringError(f"Unsupported ai_triage_mode: {raw_mode!r}")

    if human_verified:
        should_run_ai = False
        trigger_reason = "human_verified"
        crash_report.status = "ANALYZED"
    elif effective_mode == "MANUAL":
        should_run_ai = False
        trigger_reason = "manual_mode"
        crash_report.status = "ANALYZED"
    elif effective_mode == "ALWAYS":
        should_run_ai = True
        trigger_reason = "always_mode"
    else:
        # SMART_TRIAGE
        if linked_issue is None:
            should_run_ai = True
            trigger_reason = "new_or_unassigned_cluster"
        elif linked_issue.is_ambiguous_stack:
            should_run_ai = True
            trigger_reason = "ambiguous_stack"
        elif linked_issue.status == "FIXED" and _is_post_fix_version(
            crash_report.app_version,
            linked_issue.fixed_in_version,
        ):
            should_run_ai = True
            trigger_reason = "potential_regression"
        else:
            should_run_ai = False
            trigger_reason = "known_unambiguous_issue"
            crash_report.status = "ANALYZED"
            crash_report.ai_verdict = "KNOWN_ISSUE"

    similar_issues = await recall_similar_issues(
        session,
        project.id,
        top_10,
        limit=5,
    )
    await session.flush()

    return ClusterResult(
        cluster=cluster,
        issue=linked_issue,
        signature_hash=sig_hash,
        signature_text=sig_text,
        normalized_frames=normalized_frames,
        is_new_cluster=is_new_cluster,
        should_run_ai=should_run_ai,
        trigger_reason=trigger_reason,
        similar_issues=similar_issues,
    )


async def apply_human_verdict(
    session: AsyncSession,
    crash_report: CrashReport,
    action: str,
    issue_id: int | None = None,
    new_issue_data: dict[str, Any] | None = None,
    root_cause: str | None = None,
    add_log_patterns: list[str] | None = None,
    is_ambiguous_stack: bool | None = None,
    commit: bool = True,
) -> CrashReport:
    """Apply a human engineer verdict and feed knowledge back into Issues and Clusters."""
    if action not in {"confirm", "assign_issue", "create_issue"}:
        raise ClusteringError(f"Invalid verdict action: {action!r}")

    await session.refresh(crash_report, with_for_update=True)
    cluster = (
        await session.get(CrashCluster, crash_report.cluster_id)
        if crash_report.cluster_id is not None
        else None
    )
    old_issue_id = crash_report.issue_id
    old_issue = await session.get(Issue, old_issue_id) if old_issue_id is not None else None
    crash_ts = _as_utc(crash_report.crash_time)

    target_issue: Issue | None = None

    if action == "confirm":
        target_issue = old_issue
        if target_issue is None and cluster is not None and cluster.issue_id is not None:
            target_issue = await session.get(Issue, cluster.issue_id)
        if target_issue is None:
            raise ClusteringError("Cannot confirm verdict: crash report has no linked issue")
        if target_issue.project_id != crash_report.project_id:
            raise ClusteringError("Target issue belongs to a different project")
        if old_issue_id != target_issue.id:
            if old_issue is not None:
                old_issue.crash_count = max(0, (old_issue.crash_count or 0) - 1)
            crash_report.issue_id = target_issue.id
            target_issue.crash_count = (target_issue.crash_count or 0) + 1
        if target_issue.status == "NEW":
            target_issue.status = "KNOWN"
            record_issue_status_event(
                session,
                issue_id=target_issue.id,
                from_status="NEW",
                to_status="KNOWN",
                source="human_verdict",
                crash_id=crash_report.id,
            )
        if _as_utc(target_issue.last_seen_at) < crash_ts:
            target_issue.last_seen_at = crash_ts

    elif action == "assign_issue":
        if issue_id is None:
            raise ClusteringError("issue_id is required when action='assign_issue'")
        target_issue = await session.get(Issue, issue_id)
        if target_issue is None or target_issue.project_id != crash_report.project_id:
            raise ClusteringError("Target issue not found in crash report project")
        if old_issue_id != target_issue.id:
            if old_issue is not None:
                old_issue.crash_count = max(0, (old_issue.crash_count or 0) - 1)
            target_issue.crash_count = (target_issue.crash_count or 0) + 1
            crash_report.issue_id = target_issue.id
        if _as_utc(target_issue.last_seen_at) < crash_ts:
            target_issue.last_seen_at = crash_ts

    elif action == "create_issue":
        if not new_issue_data or not str(new_issue_data.get("title") or "").strip():
            raise ClusteringError("new_issue_data with a non-empty title is required")
        title = str(new_issue_data["title"]).strip()[:512]
        raw_status = new_issue_data.get("status")
        status_val = "KNOWN" if raw_status is None else str(raw_status).strip().upper()
        if status_val not in VALID_ISSUE_STATUSES:
            raise ClusteringError(f"Invalid issue status: {raw_status!r}")
        raw_comp = new_issue_data.get("component")
        comp_val = (
            str(raw_comp).strip()[:128]
            if raw_comp is not None and str(raw_comp).strip()
            else None
        )
        target_issue = Issue(
            project_id=crash_report.project_id,
            title=title,
            status=status_val,
            component=comp_val,
            root_cause=new_issue_data.get("root_cause"),
            log_patterns_json=[],
            is_ambiguous_stack=bool(is_ambiguous_stack or False),
            is_pinned=False,
            crash_count=1,
            first_seen_at=crash_ts,
            last_seen_at=crash_ts,
        )
        session.add(target_issue)
        await session.flush()
        record_issue_status_event(
            session,
            issue_id=target_issue.id,
            from_status=None,
            to_status=status_val,
            source="human_verdict",
            crash_id=crash_report.id,
        )
        crash_report.issue_id = target_issue.id

    assert target_issue is not None

    crash_report.is_human_verified = True
    if crash_report.status != "ANALYZED" and not await session.scalar(
        select(CrashJob.id).where(CrashJob.crash_id == crash_report.id)
    ):
        crash_report.status = "ANALYZED"

    if root_cause is not None:
        target_issue.root_cause = root_cause

    if add_log_patterns:
        existing_patterns = list(target_issue.log_patterns_json or [])
        cleaned_new = [str(p).strip() for p in add_log_patterns if p and str(p).strip()]
        target_issue.log_patterns_json = list(dict.fromkeys([*existing_patterns, *cleaned_new]))

    if is_ambiguous_stack is not None:
        target_issue.is_ambiguous_stack = bool(is_ambiguous_stack)

    effective_ambiguous = (
        bool(is_ambiguous_stack)
        if is_ambiguous_stack is not None
        else bool(target_issue.is_ambiguous_stack)
    )

    await session.flush()
    for affected_issue_id in {old_issue_id, target_issue.id}:
        if affected_issue_id is not None:
            exact_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(CrashReport)
                        .where(CrashReport.issue_id == affected_issue_id)
                    )
                ).scalar_one()
            )
            affected_issue = await session.get(Issue, affected_issue_id)
            if affected_issue is not None:
                affected_issue.crash_count = exact_count

    if cluster is not None:
        if effective_ambiguous:
            cluster.issue_id = None
        elif action == "create_issue":
            other_issue_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(CrashReport)
                        .where(
                            CrashReport.cluster_id == cluster.id,
                            CrashReport.id != crash_report.id,
                            CrashReport.issue_id.is_not(None),
                            CrashReport.issue_id != target_issue.id,
                        )
                    )
                ).scalar_one()
            )
            if other_issue_count > 0:
                cluster.issue_id = None
            else:
                cluster.issue_id = target_issue.id
        else:
            cluster.issue_id = target_issue.id

    await session.flush()
    if commit:
        await session.commit()
        await session.refresh(crash_report)
    return crash_report
