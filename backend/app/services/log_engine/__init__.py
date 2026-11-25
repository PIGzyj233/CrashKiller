"""Streaming 256KB chunked zstd log compression, indexing, and O(log N) / <500ms search."""

from __future__ import annotations

from app.services.log_engine.engine import LogEngine
from app.services.log_engine.parsing import (
    ERROR_LEVELS,
    LEVEL_RE,
    MAX_SESSION_ID_LENGTH,
    POST_SESSION_CONTEXT_LINES,
    TIMESTAMP_RE,
    WARN_LEVELS,
    _decode_line,
    _extract_line_meta,
    _extract_line_meta_with_dt,
    _index_matches_session,
    _parse_ts,
    _session_ascii_needle,
    _split_chunk_lines,
    normalize_session_id,
)
from app.services.log_engine.prefilter_ast import NeedleSpec
from app.services.log_engine.search_plan import SearchPlan, compile_search_plan

__all__ = [
    "ERROR_LEVELS",
    "LEVEL_RE",
    "LogEngine",
    "MAX_SESSION_ID_LENGTH",
    "NeedleSpec",
    "POST_SESSION_CONTEXT_LINES",
    "SearchPlan",
    "TIMESTAMP_RE",
    "WARN_LEVELS",
    "_decode_line",
    "_extract_line_meta",
    "_extract_line_meta_with_dt",
    "_index_matches_session",
    "_parse_ts",
    "_session_ascii_needle",
    "_split_chunk_lines",
    "compile_search_plan",
    "normalize_session_id",
]
