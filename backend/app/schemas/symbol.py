"""Pydantic schemas for Breakpad symbol management and resymbolication."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SymbolRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    app_version: str
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


class SymbolUploadResponse(SymbolRead):
    deduplicated: bool


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


class ResymbolicateRequest(BaseModel):
    project_id: uuid.UUID | None = None
    debug_id: str | None = None
    crash_ids: list[uuid.UUID] | None = None


class ResymbolicateResponse(BaseModel):
    queued_count: int
    crash_ids: list[uuid.UUID]
