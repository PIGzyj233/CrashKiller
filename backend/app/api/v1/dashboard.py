"""Dashboard aggregation REST API endpoint (/api/v1/dashboard)."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
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

_DASHBOARD_CACHE: dict[tuple[str | None, int], tuple[float, DashboardResponse]] = {}
_DASHBOARD_CACHE_MAX_ENTRIES = 64


def clear_dashboard_cache() -> None:
    """Clear in-process dashboard cache (used in tests or settings changes)."""
    _DASHBOARD_CACHE.clear()


@router.get("", response_model=DashboardResponse)
async def get_dashboard(
    project_id: uuid.UUID | None = None,
    days: int = Query(default=14, ge=1, le=365),
    refresh: bool = Query(default=False),
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
    base_settings: Settings = Depends(get_app_settings),
) -> DashboardResponse:
    cache_ttl = max(0, int(getattr(base_settings, "DASHBOARD_CACHE_TTL_SECONDS", 0)))
    cache_key = (str(project_id) if project_id is not None else None, days)
    now_mono = time.monotonic()
    if cache_ttl > 0 and not refresh:
        cached = _DASHBOARD_CACHE.get(cache_key)
        if cached is not None and cached[0] > now_mono:
            return cached[1]

    crash_filter = [CrashReport.project_id == project_id] if project_id is not None else []
    issue_filter = [Issue.project_id == project_id] if project_id is not None else []
    sym_filter = [Symbol.project_id == project_id] if project_id is not None else []

    # 1. Crash status KPIs via SQL GROUP BY
    status_rows = (
        await session.execute(
            select(CrashReport.status, func.count(CrashReport.id))
            .where(*crash_filter)
            .group_by(CrashReport.status)
        )
    ).all()
    total_crashes = 0
    analyzed_crashes = 0
    pending_crashes = 0
    failed_crashes = 0
    for status_val, cnt in status_rows:
        n = int(cnt or 0)
        total_crashes += n
        if status_val == "ANALYZED":
            analyzed_crashes += n
        elif status_val == "FAILED":
            failed_crashes += n
        elif status_val in {"PENDING", "PARSING", "TRIAGING"}:
            pending_crashes += n

    # 2. Verdict distribution via SQL GROUP BY
    verdict_rows = (
        await session.execute(
            select(CrashReport.ai_verdict, func.count(CrashReport.id).label("cnt"))
            .where(CrashReport.ai_verdict.is_not(None), *crash_filter)
            .group_by(CrashReport.ai_verdict)
            .order_by(func.count(CrashReport.id).desc())
        )
    ).all()
    verdict_distribution = [
        VerdictDistributionItem(verdict=str(verdict), count=int(cnt or 0))
        for verdict, cnt in verdict_rows
        if verdict
    ]

    # 3. Crash trend restricted to requested window via SQL GROUP BY
    today = utcnow().date()
    start_date = today - timedelta(days=days - 1)
    start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC)
    trend_buckets: dict[str, dict[str, int]] = {
        (start_date + timedelta(days=offset)).isoformat(): {
            "count": 0,
            "analyzed": 0,
            "failed": 0,
        }
        for offset in range(days)
    }

    event_ts_col = func.coalesce(CrashReport.created_at, CrashReport.crash_time)
    bind = session.get_bind()
    is_pg = bind is not None and bind.dialect.name == "postgresql"
    day_expr = (
        func.to_char(func.timezone("UTC", event_ts_col), "YYYY-MM-DD")
        if is_pg
        else func.substr(event_ts_col, 1, 10)
    )
    trend_rows = (
        await session.execute(
            select(day_expr.label("day_key"), CrashReport.status, func.count(CrashReport.id))
            .where(event_ts_col >= start_dt, *crash_filter)
            .group_by(day_expr, CrashReport.status)
        )
    ).all()
    for day_key, status_val, cnt in trend_rows:
        if day_key in trend_buckets:
            n = int(cnt or 0)
            trend_buckets[day_key]["count"] += n
            if status_val == "ANALYZED":
                trend_buckets[day_key]["analyzed"] += n
            elif status_val == "FAILED":
                trend_buckets[day_key]["failed"] += n

    # 4. Issue status counts via SQL GROUP BY
    issue_rows = (
        await session.execute(
            select(Issue.status, func.count(Issue.id).label("cnt"))
            .where(*issue_filter)
            .group_by(Issue.status)
            .order_by(func.count(Issue.id).desc())
        )
    ).all()
    total_issues = 0
    open_issues = 0
    issue_status_distribution: list[IssueStatusDistributionItem] = []
    for st, cnt in issue_rows:
        n = int(cnt or 0)
        total_issues += n
        if st in {"NEW", "KNOWN", "REGRESSION"}:
            open_issues += n
        issue_status_distribution.append(IssueStatusDistributionItem(status=str(st), count=n))

    # 5. Total symbols count via SQL COUNT
    total_symbols = int(
        (
            await session.execute(select(func.count(Symbol.id)).where(*sym_filter))
        ).scalar_one()
        or 0
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
    storage_report = await eff_storage.get_disk_usage_report_async(
        session, db_used_bytes=db_used_bytes
    )

    response = DashboardResponse(
        kpis=DashboardKPIs(
            total_crashes=total_crashes,
            analyzed_crashes=analyzed_crashes,
            pending_crashes=pending_crashes,
            open_issues=open_issues,
            missing_symbols_count=total_missing,
            failed_crashes=failed_crashes,
            total_issues=total_issues,
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
        verdict_distribution=verdict_distribution,
        issue_status_distribution=issue_status_distribution,
        top_missing_symbols=top_missing,
        storage=storage_report,
    )

    if cache_ttl > 0:
        if len(_DASHBOARD_CACHE) >= _DASHBOARD_CACHE_MAX_ENTRIES:
            _DASHBOARD_CACHE.clear()
        _DASHBOARD_CACHE[cache_key] = (now_mono + cache_ttl, response)

    return response
