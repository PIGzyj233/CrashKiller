"""Pydantic schemas for compressed crash log summaries, lines, and search."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CrashLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    crash_id: uuid.UUID
    filename: str
    raw_size: int
    compressed_size: int
    total_lines: int
    digest_json: dict[str, Any] = Field(default_factory=dict)


class LogSummaryResponse(BaseModel):
    id: uuid.UUID
    crash_id: uuid.UUID
    filename: str
    raw_size: int
    compressed_size: int
    total_lines: int
    digest: dict[str, Any] = Field(default_factory=dict)


class LogLine(BaseModel):
    line_number: int
    text: str
    timestamp: str | None = None
    level: str | None = None


class LogLinesResponse(BaseModel):
    log_id: uuid.UUID
    total_lines: int
    start_line: int
    end_line: int
    lines: list[LogLine]


class LogAroundTimeResponse(BaseModel):
    log_id: uuid.UUID
    center_timestamp: str
    window_sec: int
    start_line: int
    end_line: int
    session_id: str | None = None
    lines: list[LogLine]


class LogSessionLinesResponse(BaseModel):
    log_id: uuid.UUID
    session_id: str
    total_matches: int
    start_index: int
    end_index: int
    lines: list[LogLine]


class LogSearchMatch(BaseModel):
    line_number: int
    text: str
    timestamp: str | None = None
    level: str | None = None


class LogSearchResponse(BaseModel):
    log_id: uuid.UUID
    pattern: str
    total_matches: int
    truncated: bool
    elapsed_ms: float
    session_id: str | None = None
    matches: list[LogSearchMatch]
