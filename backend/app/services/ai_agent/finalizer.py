"""Persistence and atomic issue/cluster reconciliation for completed AI triage runs."""

from __future__ import annotations

from typing import Any

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import AIAnalysisRun, CrashCluster, CrashReport, Issue
from app.services.ai_agent.output_trimming import _as_utc
from app.services.ai_agent.prompts import _INT32_MAX
from app.services.clustering import _compare_versions
from app.services.issue_workflow import record_issue_status_event


async def finalize_triage_run(
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
    """Reconcile issue/cluster links, status transitions, and persist AIAnalysisRun."""
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
                                record_issue_status_event(
                                    session,
                                    issue_id=matched_issue.id,
                                    from_status="FIXED",
                                    to_status="REGRESSION",
                                    source="ai_regression",
                                    fixed_in_version=matched_issue.fixed_in_version,
                                    note=f"Detected in app version {crash_report.app_version}",
                                    crash_id=crash_report.id,
                                )
                        else:
                            if verdict == "REGRESSION":
                                verdict = "KNOWN_ISSUE"
                                result_dict["verdict"] = "KNOWN_ISSUE"
                    elif verdict == "REGRESSION" and matched_issue.status == "FIXED":
                        matched_issue.status = "REGRESSION"
                        record_issue_status_event(
                            session,
                            issue_id=matched_issue.id,
                            from_status="FIXED",
                            to_status="REGRESSION",
                            source="ai_regression",
                            fixed_in_version=matched_issue.fixed_in_version,
                            note=f"Detected in app version {crash_report.app_version}",
                            crash_id=crash_report.id,
                        )
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
                if final_issue_id == new_issue.id:
                    record_issue_status_event(
                        session,
                        issue_id=new_issue.id,
                        from_status=None,
                        to_status="NEW",
                        source="ai_triage",
                        crash_id=crash_report.id,
                    )
                result_dict["matched_issue_id"] = final_issue_id
            elif crash_report.issue_id is not None:
                await session.delete(new_issue)
                should_write_verdict = False
                result_dict["matched_issue_id"] = crash_report.issue_id
            else:
                await session.refresh(new_issue, attribute_names=["crash_count"])
                record_issue_status_event(
                    session,
                    issue_id=new_issue.id,
                    from_status=None,
                    to_status="NEW",
                    source="ai_triage",
                    crash_id=crash_report.id,
                )
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
