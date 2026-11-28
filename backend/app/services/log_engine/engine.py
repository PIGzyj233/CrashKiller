"""LogEngine facade over chunked zstd log ingestion, random access, and fast search."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from app.services.log_engine.ingest import ingest_log as _ingest_log
from app.services.log_engine.reader import (
    load_idx as _load_idx_file,
)
from app.services.log_engine.reader import (
    read_around_time as _read_around_time,
)
from app.services.log_engine.reader import (
    read_lines as _read_lines,
)
from app.services.log_engine.reader import (
    read_session_lines as _read_session_lines,
)
from app.services.log_engine.searcher import search_log as _search_log


class LogEngine:
    """Chunked seekable zstd log storage, indexing, random access, and fast regex search."""

    def ingest_log(
        self,
        input_path_or_stream: Path | str | BinaryIO,
        zst_path: Path | str,
        idx_path: Path | str,
        chunk_size_bytes: int = 262144,
        zstd_level: int = 6,
        session_id: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Stream-ingest a raw log into 256KB newline-aligned zstd frames + .idx.json."""
        return _ingest_log(
            input_path_or_stream,
            zst_path,
            idx_path,
            chunk_size_bytes=chunk_size_bytes,
            zstd_level=zstd_level,
            session_id=session_id,
            cancel_event=cancel_event,
        )

    def _load_idx(self, idx_path: Path | str) -> dict[str, Any]:
        return _load_idx_file(idx_path)

    def read_lines(
        self,
        zst_path: Path | str,
        idx_path: Path | str,
        start_line: int,
        end_line: int,
    ) -> list[dict[str, Any]]:
        """Read inclusive [start_line, end_line] using O(log N) chunk binary search."""
        return _read_lines(
            zst_path,
            idx_path,
            start_line,
            end_line,
            load_idx_fn=self._load_idx,
        )

    def read_session_lines(
        self,
        zst_path: Path | str,
        idx_path: Path | str,
        session_id: str,
        start_index: int = 1,
        end_index: int = 200,
    ) -> dict[str, Any]:
        """Read 1-based inclusive [start_index, end_index] of lines matching session_id."""
        return _read_session_lines(
            zst_path,
            idx_path,
            session_id,
            start_index=start_index,
            end_index=end_index,
            load_idx_fn=self._load_idx,
        )

    def read_around_time(
        self,
        zst_path: Path | str,
        idx_path: Path | str,
        target_time: datetime | str,
        window_seconds: int = 10,
        max_lines: int = 500,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read log lines within [target_time - window_seconds, target_time + window_seconds]."""
        return _read_around_time(
            zst_path,
            idx_path,
            target_time,
            window_seconds=window_seconds,
            max_lines=max_lines,
            session_id=session_id,
            load_idx_fn=self._load_idx,
        )

    def search(
        self,
        zst_path: Path | str,
        idx_path: Path | str,
        pattern: str,
        is_regex: bool = False,
        max_matches: int = 100,
        case_sensitive: bool = False,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Stream-search compressed log chunks using two-stage bytes prefiltering."""
        return _search_log(
            zst_path,
            idx_path,
            pattern,
            is_regex=is_regex,
            max_matches=max_matches,
            case_sensitive=case_sensitive,
            session_id=session_id,
            load_idx_fn=self._load_idx,
        )
