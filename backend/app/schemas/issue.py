"""Pydantic schemas for issues and crash clusters."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from app.schemas.crash import CrashSummary

IssueStatusLiteral = Literal["NEW", "KNOWN", "FIXED", "WONTFIX", "REGRESSION"]
IssueStatusEventSourceLiteral = Literal[
    "manual",
    "batch",
    "form_edit",
    "ai_triage",
    "ai_regression",
    "human_verdict",
    "split",
]


class CrashClusterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    issue_id: int | None = None
    signature_hash: str
    signature_frames_json: list[Any] = Field(default_factory=list)
    signature_text: str = ""
    exception_type: str | None = None
    crash_count: int = 0


class ClusterListResponse(BaseModel):
    items: list[CrashClusterRead]
    total: int


class IssueRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: uuid.UUID
    title: str
    status: IssueStatusLiteral
    component: str | None = None
    root_cause: str | None = None
    log_patterns_json: list[str] = Field(default_factory=list)
    fixed_in_version: str | None = None
    is_ambiguous_stack: bool = False
    is_pinned: bool = False
    crash_count: int = 0
    first_seen_at: datetime
    last_seen_at: datetime


class IssueListResponse(BaseModel):
    items: list[IssueRead]
    total: int


class IssueCreateRequest(BaseModel):
    project_id: uuid.UUID
    title: str = Field(min_length=1, max_length=512)
    status: IssueStatusLiteral = "NEW"
    component: str | None = Field(default=None, max_length=128)
    root_cause: str | None = None
    log_patterns_json: list[str] = Field(default_factory=list)
    fixed_in_version: str | None = Field(default=None, max_length=128)
    is_ambiguous_stack: bool = False
    is_pinned: bool = False

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Issue title must not be empty")
        return cleaned


class IssueUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=512)
    status: IssueStatusLiteral | None = None
    component: str | None = Field(default=None, max_length=128)
    root_cause: str | None = None
    log_patterns_json: list[str] | None = None
    fixed_in_version: str | None = Field(default=None, max_length=128)
    is_ambiguous_stack: bool | None = None
    is_pinned: bool | None = None

    @field_validator("title")
    @classmethod
    def _strip_optional_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Issue title must not be empty")
        return cleaned


class IssueMergeRequest(BaseModel):
    target_issue_id: int
    source_issue_ids: list[int] = Field(min_length=1)


class IssueSplitRequest(BaseModel):
    cluster_id: uuid.UUID
    target_issue_id: int | None = None
    new_issue_title: str | None = Field(default=None, max_length=512)


class IssueSplitResponse(BaseModel):
    source_issue: IssueRead
    target_issue: IssueRead
    cluster: CrashClusterRead


class IssueStatusEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    issue_id: int
    from_status: IssueStatusLiteral | None = None
    to_status: IssueStatusLiteral
    source: IssueStatusEventSourceLiteral
    fixed_in_version: str | None = None
    note: str | None = None
    crash_id: uuid.UUID | None = None
    created_at: datetime


class IssueTransitionRequest(BaseModel):
    to_status: IssueStatusLiteral
    fixed_in_version: str | None = Field(default=None, max_length=128)
    clear_fixed_in_version: bool = False
    note: str | None = Field(default=None, max_length=1000)


class IssueTransitionResponse(BaseModel):
    issue: IssueRead
    event: IssueStatusEventRead


class IssueBatchTransitionRequest(BaseModel):
    issue_ids: list[int] = Field(min_length=1, max_length=200)
    to_status: IssueStatusLiteral
    fixed_in_version: str | None = Field(default=None, max_length=128)
    clear_fixed_in_version: bool = False
    note: str | None = Field(default=None, max_length=1000)


class IssueBatchTransitionResponse(BaseModel):
    updated_count: int
    skipped_count: int
    items: list[IssueRead] = Field(default_factory=list)


class IssueDetailRead(IssueRead):
    clusters: list[CrashClusterRead] = Field(default_factory=list)
    recent_crashes: list[CrashSummary] = Field(default_factory=list)
    status_events: list[IssueStatusEventRead] = Field(default_factory=list)
