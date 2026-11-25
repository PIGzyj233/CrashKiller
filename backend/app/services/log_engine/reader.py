"""Indexed random-access log readers (by line range, session, and timestamp window)."""

from __future__ import annotations

import json
from bisect import bisect_left
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import zstandard as zstd

from app.services.log_engine.parsing import (
    _extract_line_meta,
    _index_matches_session,
    _parse_ts,
    _session_ascii_needle,
    _split_chunk_lines,
    normalize_session_id,
)


def load_idx(idx_path: Path | str) -> dict[str, Any]:
    with open(idx_path, encoding="utf-8") as fh:
        return json.load(fh)


def read_lines(
    zst_path: Path | str,
    idx_path: Path | str,
    start_line: int,
    end_line: int,
    *,
    load_idx_fn: Callable[[Path | str], dict[str, Any]] = load_idx,
) -> list[dict[str, Any]]:
    """Read inclusive [start_line, end_line] using O(log N) chunk binary search."""
    idx_data = load_idx_fn(idx_path)
    total_lines = int(idx_data.get("total_lines", 0))
    chunks: list[dict[str, Any]] = idx_data.get("chunks", [])
    if total_lines == 0 or not chunks:
        return []

    start = max(1, int(start_line))
    end = min(total_lines, int(end_line))
    if start > end:
        return []

    end_lines = [int(c["end_line"]) for c in chunks]
    chunk_idx = bisect_left(end_lines, start)
    decompressor = zstd.ZstdDecompressor()
    results: list[dict[str, Any]] = []

    with open(zst_path, "rb") as zst_fh:
        while chunk_idx < len(chunks):
            chunk = chunks[chunk_idx]
            c_start = int(chunk["start_line"])
            c_end = int(chunk["end_line"])
            if c_start > end:
                break

            zst_fh.seek(int(chunk["byte_offset"]))
            comp_bytes = zst_fh.read(int(chunk["compressed_size"]))
            raw_text = decompressor.decompress(comp_bytes).decode("utf-8")
            lines = _split_chunk_lines(raw_text)

            for offset, line_text in enumerate(lines):
                ln = c_start + offset
                if ln < start:
                    continue
                if ln > end:
                    break
                ts_iso, level = _extract_line_meta(line_text)
                results.append(
                    {
                        "line_number": ln,
                        "text": line_text,
                        "timestamp": ts_iso,
                        "level": level,
                    }
                )
            if c_end >= end:
                break
            chunk_idx += 1

    return results


def read_session_lines(
    zst_path: Path | str,
    idx_path: Path | str,
    session_id: str,
    start_index: int = 1,
    end_index: int = 200,
    *,
    load_idx_fn: Callable[[Path | str], dict[str, Any]] = load_idx,
) -> dict[str, Any]:
    """Read 1-based inclusive [start_index, end_index] of lines matching session_id."""
    cleaned_session_id = normalize_session_id(session_id)
    if cleaned_session_id is None:
        raise ValueError("session_id must not be empty")

    req_start = int(start_index)
    req_end = int(end_index)
    if req_start < 1 or req_end < 1 or req_start > req_end:
        raise ValueError("start_index and end_index must be >= 1 and start_index <= end_index")

    idx_data = load_idx_fn(idx_path)
    chunks: list[dict[str, Any]] = idx_data.get("chunks", [])
    if not chunks:
        return {
            "session_id": cleaned_session_id,
            "total_matches": 0,
            "start_index": 0,
            "end_index": 0,
            "lines": [],
        }

    session_lower = cleaned_session_id.lower()
    ascii_needle = _session_ascii_needle(session_lower)
    decompressor = zstd.ZstdDecompressor()
    results: list[dict[str, Any]] = []

    use_indexed = _index_matches_session(idx_data, session_lower) and all(
        "session_match_count" in c for c in chunks
    )
    if use_indexed:
        cum_counts: list[int] = []
        running_total = 0
        for c in chunks:
            running_total += int(c.get("session_match_count", 0))
            cum_counts.append(running_total)
        total_matches = running_total
        if total_matches == 0 or req_start > total_matches:
            return {
                "session_id": cleaned_session_id,
                "total_matches": total_matches,
                "start_index": 0,
                "end_index": 0,
                "lines": [],
            }
        clamped_end = min(total_matches, req_end)
        chunk_idx = bisect_left(cum_counts, req_start)
        match_cursor = cum_counts[chunk_idx - 1] if chunk_idx > 0 else 0

        with open(zst_path, "rb") as zst_fh:
            while chunk_idx < len(chunks) and match_cursor < clamped_end:
                chunk = chunks[chunk_idx]
                c_matches = int(chunk.get("session_match_count", 0))
                if c_matches <= 0:
                    chunk_idx += 1
                    continue
                c_start = int(chunk["start_line"])
                zst_fh.seek(int(chunk["byte_offset"]))
                comp_bytes = zst_fh.read(int(chunk["compressed_size"]))
                raw_text = decompressor.decompress(comp_bytes).decode("utf-8")
                for offset, line_text in enumerate(_split_chunk_lines(raw_text)):
                    if session_lower in line_text.lower():
                        match_cursor += 1
                        if match_cursor < req_start:
                            continue
                        if match_cursor > clamped_end:
                            break
                        ts_iso, level = _extract_line_meta(line_text)
                        results.append(
                            {
                                "line_number": c_start + offset,
                                "text": line_text,
                                "timestamp": ts_iso,
                                "level": level,
                            }
                        )
                chunk_idx += 1
    else:
        total_matches = 0
        with open(zst_path, "rb") as zst_fh:
            for chunk in chunks:
                zst_fh.seek(int(chunk["byte_offset"]))
                comp_bytes = zst_fh.read(int(chunk["compressed_size"]))
                chunk_bytes = decompressor.decompress(comp_bytes)
                if ascii_needle is not None and ascii_needle not in chunk_bytes.lower():
                    continue
                c_start = int(chunk["start_line"])
                raw_text = chunk_bytes.decode("utf-8")
                for offset, line_text in enumerate(_split_chunk_lines(raw_text)):
                    if session_lower in line_text.lower():
                        total_matches += 1
                        if req_start <= total_matches <= req_end:
                            ts_iso, level = _extract_line_meta(line_text)
                            results.append(
                                {
                                    "line_number": c_start + offset,
                                    "text": line_text,
                                    "timestamp": ts_iso,
                                    "level": level,
                                }
                            )

    if results:
        resp_start = req_start
        resp_end = req_start + len(results) - 1
    else:
        resp_start = 0
        resp_end = 0

    return {
        "session_id": cleaned_session_id,
        "total_matches": total_matches,
        "start_index": resp_start,
        "end_index": resp_end,
        "lines": results,
    }


def read_around_time(
    zst_path: Path | str,
    idx_path: Path | str,
    target_time: datetime | str,
    window_seconds: int = 10,
    max_lines: int = 500,
    session_id: str | None = None,
    *,
    load_idx_fn: Callable[[Path | str], dict[str, Any]] = load_idx,
) -> list[dict[str, Any]]:
    """Read log lines within [target_time - window_seconds, target_time + window_seconds]."""
    cleaned_session_id = normalize_session_id(session_id)
    session_lower = cleaned_session_id.lower() if cleaned_session_id is not None else None
    ascii_needle = _session_ascii_needle(session_lower) if session_lower is not None else None

    if isinstance(target_time, str):
        target_dt = _parse_ts(target_time)
    elif target_time.tzinfo is None:
        target_dt = target_time.replace(tzinfo=UTC)
    else:
        target_dt = target_time.astimezone(UTC)
    if target_dt is None:
        raise ValueError(f"Invalid target_time: {target_time!r}")

    idx_data = load_idx_fn(idx_path)
    chunks: list[dict[str, Any]] = idx_data.get("chunks", [])
    if not chunks:
        return []

    can_skip_by_idx = (
        session_lower is not None and _index_matches_session(idx_data, session_lower)
    )

    delta = timedelta(seconds=max(0, window_seconds))
    start_dt = target_dt - delta
    end_dt = target_dt + delta

    # Compute per-chunk [min_ts, max_ts] bounds and monotonic prefix_max / suffix_min
    chunk_bounds: list[tuple[datetime | None, datetime | None]] = []
    timed_indices: list[int] = []
    timed_prefix_max: list[datetime] = []
    running_max: datetime | None = None

    for idx, c in enumerate(chunks):
        f_ts = _parse_ts(c["first_ts"]) if c.get("first_ts") else None
        l_ts = _parse_ts(c["last_ts"]) if c.get("last_ts") else None
        min_ts = _parse_ts(c["min_ts"]) if c.get("min_ts") else None
        max_ts = _parse_ts(c["max_ts"]) if c.get("max_ts") else None
        if min_ts is None:
            if f_ts is not None and l_ts is not None:
                min_ts = min(f_ts, l_ts)
            else:
                min_ts = f_ts or l_ts
        if max_ts is None:
            if f_ts is not None and l_ts is not None:
                max_ts = max(f_ts, l_ts)
            else:
                max_ts = l_ts or f_ts
        chunk_bounds.append((min_ts, max_ts))
        if max_ts is not None:
            if running_max is None or max_ts > running_max:
                running_max = max_ts
            timed_indices.append(idx)
            timed_prefix_max.append(running_max)

    if not timed_indices:
        return []

    suffix_min: list[datetime | None] = [None] * len(chunks)
    running_min: datetime | None = None
    for idx in range(len(chunks) - 1, -1, -1):
        c_min = chunk_bounds[idx][0]
        if c_min is not None:
            if running_min is None or c_min < running_min:
                running_min = c_min
        suffix_min[idx] = running_min

    pos = bisect_left(timed_prefix_max, start_dt)
    if pos >= len(timed_indices):
        return []
    first_chunk_idx = max(0, timed_indices[pos] - 1)
    # Walk backwards over immediately preceding untimed chunks
    while first_chunk_idx > 0 and chunk_bounds[first_chunk_idx][0] is None:
        first_chunk_idx -= 1

    candidate_indices: list[int] = []
    for idx in range(first_chunk_idx, len(chunks)):
        c_min, c_max = chunk_bounds[idx]
        is_untimed = c_min is None and c_max is None
        s_min = suffix_min[idx]
        if not is_untimed and s_min is not None and s_min > end_dt:
            break
        if c_min is not None and c_min > end_dt:
            continue
        if c_max is not None and c_max < start_dt:
            continue
        candidate_indices.append(idx)

    decompressor = zstd.ZstdDecompressor()
    matched: list[dict[str, Any]] = []
    diffs: list[float] = []
    current_ts: datetime | None = None
    prev_idx: int | None = None

    with open(zst_path, "rb") as zst_fh:
        for idx in candidate_indices:
            chunk = chunks[idx]
            if (
                can_skip_by_idx
                and "session_match_count" in chunk
                and int(chunk["session_match_count"]) == 0
            ):
                continue
            if prev_idx is not None and idx != prev_idx + 1:
                current_ts = None
            prev_idx = idx
            c_start = int(chunk["start_line"])
            zst_fh.seek(int(chunk["byte_offset"]))
            comp_bytes = zst_fh.read(int(chunk["compressed_size"]))
            chunk_bytes = decompressor.decompress(comp_bytes)
            if ascii_needle is not None and ascii_needle not in chunk_bytes.lower():
                continue
            raw_text = chunk_bytes.decode("utf-8")

            for offset, line_text in enumerate(_split_chunk_lines(raw_text)):
                ts_iso, level = _extract_line_meta(line_text)
                if ts_iso is not None:
                    line_dt = _parse_ts(ts_iso)
                    current_ts = line_dt
                else:
                    line_dt = current_ts

                if line_dt is None:
                    continue
                if start_dt <= line_dt <= end_dt:
                    if session_lower is not None and session_lower not in line_text.lower():
                        continue
                    matched.append(
                        {
                            "line_number": c_start + offset,
                            "text": line_text,
                            "timestamp": ts_iso,
                            "level": level,
                        }
                    )
                    diffs.append(abs((line_dt - target_dt).total_seconds()))

    if max_lines > 0 and len(matched) > max_lines:
        center_idx = min(range(len(diffs)), key=lambda i: diffs[i])
        half = max_lines // 2
        slice_start = max(0, center_idx - half)
        slice_end = slice_start + max_lines
        if slice_end > len(matched):
            slice_end = len(matched)
            slice_start = max(0, slice_end - max_lines)
        matched = matched[slice_start:slice_end]

    return matched
