"""Pydantic schemas for Breakpad symbol management, public symbol store, and resymbolication."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SymbolScope = Literal["project", "public"]


class SymbolRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID | None = None
    app_version: str | None = None
    scope: SymbolScope = "project"
    source: str = "upload"
    module_name: str
    debug_file: str
    debug_id: str
    code_file: str | None = None
    code_id: str | None = None
    has_pe_unwind: bool
    sym_size: int
    raw_size: int
    last_used_at: datetime | None = None
    created_at: datetime
    sym_path: str | None = None
    raw_archive_path: str | None = None

    @model_validator(mode="after")
    def _derive_scope(self) -> SymbolRead:
        self.scope = "public" if self.project_id is None else "project"
        return self


class SymbolUploadResponse(SymbolRead):
    deduplicated: bool
    upgraded: bool = False
    resymbolicate_queued_count: int = 0
    resymbolicate_crash_ids: list[uuid.UUID] = Field(default_factory=list)
    resymbolicate_skipped_active_count: int = 0
    resymbolicate_active_crash_ids: list[uuid.UUID] = Field(default_factory=list)


class SymbolListResponse(BaseModel):
    items: list[SymbolRead]
    total: int


class MissingSymbolQueueItem(BaseModel):
    debug_file: str = ""
    debug_id: str = ""
    module_name: str = ""
    code_file: str | None = None
    code_id: str | None = None
    crash_count: int
    latest_crash_at: datetime
    affected_crash_ids: list[uuid.UUID] = Field(default_factory=list)
    is_windows_system: bool = False
    is_available_locally: bool = False
    local_scope: SymbolScope | None = None
    msdl_negative_cached: bool = False
    msdl_last_reason: str | None = None


class PublicSymbolSyncTarget(BaseModel):
    module_name: str | None = None
    debug_file: str | None = None
    debug_id: str = Field(..., min_length=32, max_length=64)
    age: str | int | None = None
    code_file: str | None = None
    code_id: str | None = None


class PublicSymbolSyncRequest(BaseModel):
    mode: Literal["targets", "missing_windows"] = "targets"
    targets: list[PublicSymbolSyncTarget] = Field(default_factory=list)
    project_id: uuid.UUID | None = None
    force: bool = True
    auto_resymbolicate: bool = True
    max_targets: int | None = Field(default=None, ge=1, le=2000)


class PublicSymbolSyncItemResult(BaseModel):
    module_name: str
    debug_file: str
    debug_id: str
    status: Literal[
        "imported",
        "upgraded",
        "cached_hit",
        "not_found",
        "network_error",
        "skipped",
        "failed",
    ]
    detail: str | None = None
    has_pe_unwind: bool = False
    symbol_id: uuid.UUID | None = None


class PublicSymbolSyncResponse(BaseModel):
    attempted: int = 0
    imported: int = 0
    upgraded: int = 0
    cached_hit: int = 0
    not_found: int = 0
    skipped: int = 0
    failed: int = 0
    circuit_open: bool = False
    resymbolicate_queued_count: int = 0
    resymbolicate_crash_ids: list[uuid.UUID] = Field(default_factory=list)
    resymbolicate_skipped_active_count: int = 0
    resymbolicate_active_crash_ids: list[uuid.UUID] = Field(default_factory=list)
    results: list[PublicSymbolSyncItemResult] = Field(default_factory=list)


class ResymbolicateRequest(BaseModel):
    project_id: uuid.UUID | None = None
    debug_id: str | None = None
    crash_ids: list[uuid.UUID] | None = None
    missing_only: bool = False


class ResymbolicateResponse(BaseModel):
    queued_count: int
    crash_ids: list[uuid.UUID]
    skipped_active_count: int = 0
    active_crash_ids: list[uuid.UUID] = Field(default_factory=list)


class ResymbolicateStatusResponse(BaseModel):
    total: int = 0
    pending: int = 0
    parsing: int = 0
    triaging: int = 0
    analyzed: int = 0
    failed: int = 0
    completed: int = 0
    in_progress: int = 0
    done: bool = True
