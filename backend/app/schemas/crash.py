"""Pydantic schemas for crash reports, AI analysis runs, SSE, and verdicts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.issue import CrashClusterRead, IssueRead, IssueStatusLiteral
from app.schemas.log import CrashLogRead

CrashStatusLiteral = Literal["PENDING", "PARSING", "TRIAGING", "ANALYZED", "FAILED"]
AIVerdictLiteral = Literal["KNOWN_ISSUE", "NEW_ISSUE", "REGRESSION", "NEEDS_REVIEW"]
AIRunStatusLiteral = Literal["RUNNING", "COMPLETED", "FAILED"]
ReprocessModeLiteral = Literal["full", "triage"]
VerdictActionLiteral = Literal["confirm", "assign_issue", "create_issue"]


class AIAnalysisRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    crash_id: uuid.UUID
    model_name: str
    status: str
    result_json: dict[str, Any] | None = None
    trace_json: list[dict[str, Any]] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    created_at: datetime


class CrashSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    cluster_id: uuid.UUID | None = None
    issue_id: int | None = None
    status: CrashStatusLiteral
    app_version: str
    build_id: str | None = None
    os_version: str | None = None
    dump_raw_size: int = 0
    dump_compressed_size: int = 0
    crash_reason: str | None = None
    crash_address: str | None = None
    ai_verdict: AIVerdictLiteral | None = None
    ai_confidence: float | None = None
    is_human_verified: bool = False
    is_pinned: bool = False
    crash_time: datetime | None = None
    created_at: datetime


class CrashListResponse(BaseModel):
    items: list[CrashSummary]
    total: int


class CrashDetailRead(CrashSummary):
    dump_path: str | None = None
    parsed_summary_json: dict[str, Any] | None = None
    parsed_stacks_json: dict[str, Any] | None = None
    missing_symbols_json: list[dict[str, Any]] | None = None
    logs: list[CrashLogRead] = Field(default_factory=list)
    cluster: CrashClusterRead | None = None
    issue: IssueRead | None = None
    ai_runs: list[AIAnalysisRunRead] = Field(default_factory=list)


class CrashUploadResponse(BaseModel):
    crash_id: uuid.UUID
    status: str = "PENDING"
    log_ids: list[uuid.UUID] = Field(default_factory=list)


class CrashReprocessRequest(BaseModel):
    mode: ReprocessModeLiteral


class CrashReprocessResponse(BaseModel):
    crash_id: uuid.UUID
    status: str


class CrashVerdictNewIssue(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    component: str | None = Field(default=None, max_length=128)
    root_cause: str | None = None
    status: IssueStatusLiteral | None = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Issue title must not be empty")
        return cleaned


class CrashVerdictRequest(BaseModel):
    action: VerdictActionLiteral
    issue_id: int | None = None
    new_issue: CrashVerdictNewIssue | None = None
    root_cause: str | None = None
    add_log_patterns: list[str] | None = None
    is_ambiguous_stack: bool | None = None


class CrashPinRequest(BaseModel):
    is_pinned: bool


class CrashPinResponse(BaseModel):
    crash_id: uuid.UUID
    is_pinned: bool


class CrashSSEEvent(BaseModel):
    crash_id: str
    status: str
    stage: str | None = None
    ai_verdict: str | None = None
    error: str | None = None
