"""Pydantic schemas for dashboard KPIs, trends, distributions, and storage."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.schemas.symbol import MissingSymbolQueueItem


class DashboardKPIs(BaseModel):
    total_crashes: int
    analyzed_crashes: int
    pending_crashes: int
    open_issues: int
    missing_symbols_count: int
    failed_crashes: int = 0
    total_issues: int = 0
    total_symbols: int = 0


class CrashTrendPoint(BaseModel):
    date: str
    count: int
    analyzed: int = 0
    failed: int = 0


class VerdictDistributionItem(BaseModel):
    verdict: str
    count: int


class IssueStatusDistributionItem(BaseModel):
    status: str
    count: int


class DashboardResponse(BaseModel):
    kpis: DashboardKPIs
    crash_trend: list[CrashTrendPoint] = Field(default_factory=list)
    verdict_distribution: list[VerdictDistributionItem] = Field(default_factory=list)
    issue_status_distribution: list[IssueStatusDistributionItem] = Field(default_factory=list)
    top_missing_symbols: list[MissingSymbolQueueItem] = Field(default_factory=list)
    storage: dict[str, Any]
