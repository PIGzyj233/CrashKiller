"""Dashboard aggregation REST API endpoint (/api/v1/dashboard)."""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_app_settings,
    get_db,
    get_effective_storage,
    get_storage,
)
from app.api.v1.symbols import aggregate_missing_symbols
from app.config import Settings
from app.db.base import utcnow
from app.db.models import CrashReport, Issue, Symbol
from app.schemas.dashboard import (
    CrashTrendPoint,
    DashboardKPIs,
    DashboardResponse,
    IssueStatusDistributionItem,
    VerdictDistributionItem,
)
from app.services.storage import StorageManager

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


@router.get("", response_model=DashboardResponse)
async def get_dashboard(
    project_id: uuid.UUID | None = None,
    days: int = Query(default=14, ge=1, le=365),
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
    base_settings: Settings = Depends(get_app_settings),
) -> DashboardResponse:
    crash_stmt = select(
        CrashReport.status,
        CrashReport.ai_verdict,
        CrashReport.crash_time,
        CrashReport.created_at,
    )
    if project_id is not None:
        crash_stmt = crash_stmt.where(CrashReport.project_id == project_id)
    crash_rows = (await session.execute(crash_stmt)).all()

    issue_stmt = select(Issue.status)
    if project_id is not None:
        issue_stmt = issue_stmt.where(Issue.project_id == project_id)
    issue_statuses = (await session.execute(issue_stmt)).scalars().all()

    sym_stmt = select(Symbol.id)
    if project_id is not None:
        sym_stmt = sym_stmt.where(Symbol.project_id == project_id)
    total_symbols = len((await session.execute(sym_stmt)).scalars().all())

    total_crashes = len(crash_rows)
    analyzed_crashes = 0
    pending_crashes = 0
    failed_crashes = 0
    verdict_counts: Counter[str] = Counter()

    today = utcnow().date()
    start_date = today - timedelta(days=days - 1)
    trend_buckets: dict[str, dict[str, int]] = {
        (start_date + timedelta(days=offset)).isoformat(): {
            "count": 0,
            "analyzed": 0,
            "failed": 0,
        }
        for offset in range(days)
    }

    for status_val, verdict_val, crash_time, created_at in crash_rows:
        if status_val == "ANALYZED":
            analyzed_crashes += 1
        elif status_val == "FAILED":
            failed_crashes += 1
        elif status_val in {"PENDING", "PARSING", "TRIAGING"}:
            pending_crashes += 1

        if verdict_val:
            verdict_counts[verdict_val] += 1

        event_dt = _as_utc(created_at or crash_time)
        day_key = event_dt.date().isoformat()
        if day_key in trend_buckets:
            trend_buckets[day_key]["count"] += 1
            if status_val == "ANALYZED":
                trend_buckets[day_key]["analyzed"] += 1
            elif status_val == "FAILED":
                trend_buckets[day_key]["failed"] += 1

    issue_status_counts: Counter[str] = Counter(issue_statuses)
    open_issues = sum(
        count for st, count in issue_status_counts.items() if st in {"NEW", "KNOWN", "REGRESSION"}
    )

    _, eff_storage = await get_effective_storage(session, storage, base_settings)
    top_missing, total_missing = await aggregate_missing_symbols(
        session,
        project_id=project_id,
        limit=10,
        offset=0,
        storage=eff_storage,
    )

    db_used_bytes = await eff_storage.get_db_size_bytes(session)
    storage_report = await asyncio.to_thread(eff_storage.get_disk_usage_report, db_used_bytes)

    return DashboardResponse(
        kpis=DashboardKPIs(
            total_crashes=total_crashes,
            analyzed_crashes=analyzed_crashes,
            pending_crashes=pending_crashes,
            open_issues=open_issues,
            missing_symbols_count=total_missing,
            failed_crashes=failed_crashes,
            total_issues=len(issue_statuses),
            total_symbols=total_symbols,
        ),
        crash_trend=[
            CrashTrendPoint(
                date=day_key,
                count=vals["count"],
                analyzed=vals["analyzed"],
                failed=vals["failed"],
            )
            for day_key, vals in sorted(trend_buckets.items())
        ],
        verdict_distribution=[
            VerdictDistributionItem(verdict=verdict, count=count)
            for verdict, count in verdict_counts.most_common()
        ],
        issue_status_distribution=[
            IssueStatusDistributionItem(status=st, count=count)
            for st, count in issue_status_counts.most_common()
        ],
        top_missing_symbols=top_missing,
        storage=storage_report,
    )
