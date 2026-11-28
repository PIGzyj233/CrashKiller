"""Streaming chunked zstd log ingestion and index/digest generation."""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

import zstandard as zstd

from app.services.log_engine.parsing import (
    ERROR_LEVELS,
    POST_SESSION_CONTEXT_LINES,
    WARN_LEVELS,
    _decode_line,
    _extract_line_meta_with_dt,
    normalize_session_id,
)


def ingest_log(
    input_path_or_stream: Path | str | BinaryIO,
    zst_path: Path | str,
    idx_path: Path | str,
    chunk_size_bytes: int = 262144,
    zstd_level: int = 6,
    session_id: str | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Stream-ingest a raw log into 256KB newline-aligned zstd frames + .idx.json."""
    cleaned_session_id = normalize_session_id(session_id)
    session_lower = cleaned_session_id.lower() if cleaned_session_id is not None else None

    zst_dest = Path(zst_path)
    idx_dest = Path(idx_path)
    zst_dest.parent.mkdir(parents=True, exist_ok=True)
    idx_dest.parent.mkdir(parents=True, exist_ok=True)

    zst_tmp = zst_dest.with_name(f".tmp_{zst_dest.name}")
    idx_tmp = idx_dest.with_name(f".tmp_{idx_dest.name}")

    compressor = zstd.ZstdCompressor(level=zstd_level)
    chunks_meta: list[dict[str, Any]] = []
    tail_lines: deque[dict[str, Any]] = deque(maxlen=100)
    recent_errors: deque[dict[str, Any]] = deque(maxlen=50)

    session_tail_lines: deque[dict[str, Any]] = deque(maxlen=100)
    session_recent_errors: deque[dict[str, Any]] = deque(maxlen=50)
    post_session_lines: list[dict[str, Any]] = []
    session_match_count = 0
    session_error_count = 0
    session_warn_count = 0
    session_first_line: int | None = None
    session_last_line: int | None = None
    session_first_ts: str | None = None
    session_last_ts: str | None = None

    raw_size = 0
    compressed_size = 0
    total_lines = 0

    chunk_buf = bytearray()
    chunk_start_line = 1
    chunk_first_ts: str | None = None
    chunk_last_ts: str | None = None
    chunk_min_dt: datetime | None = None
    chunk_min_ts: str | None = None
    chunk_max_dt: datetime | None = None
    chunk_max_ts: str | None = None
    chunk_error_count = 0
    chunk_warn_count = 0
    chunk_session_match_count = 0

    def _flush_chunk(zst_fh: BinaryIO) -> None:
        nonlocal compressed_size
        nonlocal chunk_start_line, chunk_first_ts, chunk_last_ts
        nonlocal chunk_min_dt, chunk_min_ts, chunk_max_dt, chunk_max_ts
        nonlocal chunk_error_count, chunk_warn_count, chunk_session_match_count
        if not chunk_buf:
            return
        frame = compressor.compress(bytes(chunk_buf))
        byte_offset = compressed_size
        zst_fh.write(frame)
        frame_len = len(frame)
        compressed_size += frame_len
        meta: dict[str, Any] = {
            "chunk_id": len(chunks_meta),
            "byte_offset": byte_offset,
            "compressed_size": frame_len,
            "uncompressed_size": len(chunk_buf),
            "start_line": chunk_start_line,
            "end_line": total_lines,
            "first_ts": chunk_first_ts,
            "last_ts": chunk_last_ts,
            "min_ts": chunk_min_ts,
            "max_ts": chunk_max_ts,
            "error_count": chunk_error_count,
            "warn_count": chunk_warn_count,
        }
        if cleaned_session_id is not None:
            meta["session_match_count"] = chunk_session_match_count
        chunks_meta.append(meta)
        chunk_buf.clear()
        chunk_start_line = total_lines + 1
        chunk_first_ts = None
        chunk_last_ts = None
        chunk_min_dt = None
        chunk_min_ts = None
        chunk_max_dt = None
        chunk_max_ts = None
        chunk_error_count = 0
        chunk_warn_count = 0
        chunk_session_match_count = 0

    def _process_raw_line(raw_line: bytes, zst_fh: BinaryIO) -> None:
        nonlocal total_lines, chunk_first_ts, chunk_last_ts
        nonlocal chunk_min_dt, chunk_min_ts, chunk_max_dt, chunk_max_ts
        nonlocal chunk_error_count, chunk_warn_count, chunk_session_match_count
        nonlocal session_match_count, session_error_count, session_warn_count
        nonlocal session_first_line, session_last_line, session_first_ts, session_last_ts

        if total_lines == 0 and raw_line.startswith(b"\xef\xbb\xbf"):
            raw_line = raw_line[3:]
        if raw_line.endswith(b"\r"):
            raw_line = raw_line[:-1]

        total_lines += 1
        text = _decode_line(raw_line)
        ts_iso, ts_dt, level = _extract_line_meta_with_dt(text)

        if ts_iso is not None:
            if chunk_first_ts is None:
                chunk_first_ts = ts_iso
            chunk_last_ts = ts_iso
            if ts_dt is not None:
                if chunk_min_dt is None or ts_dt < chunk_min_dt:
                    chunk_min_dt = ts_dt
                    chunk_min_ts = ts_iso
                if chunk_max_dt is None or ts_dt > chunk_max_dt:
                    chunk_max_dt = ts_dt
                    chunk_max_ts = ts_iso

        entry = {
            "line_number": total_lines,
            "text": text,
            "timestamp": ts_iso,
            "level": level,
        }

        if level in ERROR_LEVELS:
            chunk_error_count += 1
            recent_errors.append(entry)
        elif level in WARN_LEVELS:
            chunk_warn_count += 1

        tail_lines.append(entry)

        if session_lower is not None:
            if session_lower in text.lower():
                chunk_session_match_count += 1
                session_match_count += 1
                if session_first_line is None:
                    session_first_line = total_lines
                session_last_line = total_lines
                if ts_iso is not None:
                    if session_first_ts is None:
                        session_first_ts = ts_iso
                    session_last_ts = ts_iso
                if level in ERROR_LEVELS:
                    session_error_count += 1
                    session_recent_errors.append(entry)
                elif level in WARN_LEVELS:
                    session_warn_count += 1
                session_tail_lines.append(entry)
                post_session_lines.clear()
            elif (
                session_last_line is not None
                and len(post_session_lines) < POST_SESSION_CONTEXT_LINES
            ):
                post_session_lines.append(entry)

        encoded = (text + "\n").encode("utf-8")
        chunk_buf.extend(encoded)
        if len(chunk_buf) >= chunk_size_bytes:
            _flush_chunk(zst_fh)

    close_src = False
    if isinstance(input_path_or_stream, (str, Path)):
        src_fh: BinaryIO = open(input_path_or_stream, "rb")
        close_src = True
    else:
        src_fh = input_path_or_stream

    try:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Log ingestion cancelled")
        with open(zst_tmp, "wb") as zst_fh:
            pending = bytearray()
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("Log ingestion cancelled")
                block = src_fh.read(65536)
                if not block:
                    break
                raw_size += len(block)
                pending.extend(block)
                start = 0
                while True:
                    nl_pos = pending.find(b"\n", start)
                    if nl_pos == -1:
                        if start > 0:
                            del pending[:start]
                        break
                    _process_raw_line(bytes(pending[start:nl_pos]), zst_fh)
                    start = nl_pos + 1

            if pending:
                _process_raw_line(bytes(pending), zst_fh)
                pending.clear()

            _flush_chunk(zst_fh)
            zst_fh.flush()
            os.fsync(zst_fh.fileno())
    except BaseException:
        if zst_tmp.exists():
            zst_tmp.unlink()
        raise
    finally:
        if close_src:
            src_fh.close()

    digest_json: dict[str, Any] = {
        "total_lines": total_lines,
        "error_count": sum(c["error_count"] for c in chunks_meta),
        "warn_count": sum(c["warn_count"] for c in chunks_meta),
        "tail_lines": list(tail_lines),
        "recent_errors": list(recent_errors),
    }
    if cleaned_session_id is not None:
        digest_json["session"] = {
            "session_id": cleaned_session_id,
            "match_count": session_match_count,
            "error_count": session_error_count,
            "warn_count": session_warn_count,
            "first_line": session_first_line,
            "last_line": session_last_line,
            "first_ts": session_first_ts,
            "last_ts": session_last_ts,
            "trailing_lines_after_session": (
                max(0, total_lines - session_last_line)
                if session_last_line is not None
                else 0
            ),
            "tail_lines": list(session_tail_lines),
            "recent_errors": list(session_recent_errors),
            "post_session_lines": post_session_lines,
        }

    idx_data: dict[str, Any] = {
        "version": 1,
        "chunk_size_bytes": chunk_size_bytes,
        "raw_size": raw_size,
        "compressed_size": compressed_size,
        "total_lines": total_lines,
        "chunks": chunks_meta,
        "digest": digest_json,
    }
    if cleaned_session_id is not None:
        idx_data["session_id"] = cleaned_session_id

    try:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Log ingestion cancelled")
        with open(idx_tmp, "w", encoding="utf-8") as idx_fh:
            json.dump(idx_data, idx_fh, ensure_ascii=False)
            idx_fh.flush()
            os.fsync(idx_fh.fileno())
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Log ingestion cancelled")
        os.replace(zst_tmp, zst_dest)
        os.replace(idx_tmp, idx_dest)
    except BaseException:
        for p in (zst_tmp, idx_tmp):
            if p.exists():
                p.unlink()
        raise

    return {
        "raw_size": raw_size,
        "compressed_size": compressed_size,
        "total_lines": total_lines,
        "digest_json": digest_json,
        "idx_data": idx_data,
    }
