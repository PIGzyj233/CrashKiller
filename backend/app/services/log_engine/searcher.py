"""Two-stage streaming search execution over chunked zstd logs."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import zstandard as zstd

from app.services.log_engine.parsing import (
    _index_matches_session,
    _session_ascii_needle,
    _split_chunk_lines,
    normalize_session_id,
)
from app.services.log_engine.reader import load_idx
from app.services.log_engine.search_plan import compile_search_plan


def search_log(
    zst_path: Path | str,
    idx_path: Path | str,
    pattern: str,
    is_regex: bool = False,
    max_matches: int = 100,
    case_sensitive: bool = False,
    session_id: str | None = None,
    *,
    load_idx_fn: Callable[[Path | str], dict[str, Any]] = load_idx,
) -> dict[str, Any]:
    """Stream-search compressed log chunks using two-stage bytes prefiltering."""
    if not pattern:
        raise ValueError("Search pattern must not be empty")
    if max_matches <= 0:
        raise ValueError("max_matches must be positive")

    cleaned_session_id = normalize_session_id(session_id)
    session_lower = cleaned_session_id.lower() if cleaned_session_id is not None else None
    session_ascii_needle = (
        _session_ascii_needle(session_lower) if session_lower is not None else None
    )

    t0 = time.perf_counter()
    idx_data = load_idx_fn(idx_path)
    chunks: list[dict[str, Any]] = idx_data.get("chunks", [])

    if not chunks:
        return {
            "total_matches": 0,
            "truncated": False,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "scanned_chunks": 0,
            "matches": [],
        }

    can_skip_by_idx = (
        session_lower is not None and _index_matches_session(idx_data, session_lower)
    )

    plan = compile_search_plan(pattern, is_regex, case_sensitive)
    exact_re = plan.exact_re
    precheck_mode = plan.precheck_mode
    multiline_risk = plan.multiline_risk

    decompressor = zstd.ZstdDecompressor()
    matches: list[dict[str, Any]] = []
    truncated = False
    scanned_chunks = 0

    with open(zst_path, "rb") as zst_fh:
        for chunk in chunks:
            if (
                can_skip_by_idx
                and "session_match_count" in chunk
                and int(chunk["session_match_count"]) == 0
            ):
                continue
            scanned_chunks += 1
            zst_fh.seek(int(chunk["byte_offset"]))
            comp_bytes = zst_fh.read(int(chunk["compressed_size"]))
            chunk_bytes = decompressor.decompress(comp_bytes)

            chunk_lower_bytes: bytes | None = None
            if session_ascii_needle is not None:
                chunk_lower_bytes = chunk_bytes.lower()
                if session_ascii_needle not in chunk_lower_bytes:
                    continue

            # Stage 1: fast C-speed bytes prefilter
            if precheck_mode == "literal_exact":
                if plan.needle_bytes not in chunk_bytes:
                    continue
            elif precheck_mode == "literal_lower":
                if chunk_lower_bytes is None:
                    chunk_lower_bytes = chunk_bytes.lower()
                if plan.needle_bytes not in chunk_lower_bytes:
                    continue
            elif precheck_mode == "literals_dnf":
                low_bytes: bytes | None = chunk_lower_bytes
                branch_hit = False
                for branch in plan.dnf_branches:
                    ok = True
                    for spec in branch:
                        if spec.use_lower:
                            if low_bytes is None:
                                low_bytes = chunk_bytes.lower()
                            haystack = low_bytes
                        else:
                            haystack = chunk_bytes
                        if not any(v in haystack for v in spec.variants):
                            ok = False
                            break
                    if ok:
                        branch_hit = True
                        break
                if not branch_hit:
                    continue
            elif precheck_mode == "regex_bytes":
                assert plan.bytes_re is not None
                if not plan.bytes_re.search(chunk_bytes):
                    continue
            elif precheck_mode == "regex_bytes_lower":
                assert plan.bytes_re is not None
                if chunk_lower_bytes is None:
                    chunk_lower_bytes = chunk_bytes.lower()
                if not plan.bytes_re.search(chunk_lower_bytes):
                    continue

            # Stage 2: exact UTF-8 line matching on hit chunks only
            text = chunk_bytes.decode("utf-8")
            c_start = int(chunk["start_line"])
            c_end = int(chunk["end_line"])

            if multiline_risk:
                for offset, line_text in enumerate(_split_chunk_lines(text)):
                    if session_lower is not None and session_lower not in line_text.lower():
                        continue
                    if exact_re.search(line_text):
                        if len(matches) >= max_matches:
                            truncated = True
                            break
                        matches.append(
                            {
                                "line_number": c_start + offset,
                                "text": line_text,
                            }
                        )
            else:
                search_text = text[:-1] if text.endswith("\n") else text
                prev_pos = 0
                cur_line_offset = 0
                last_matched_line = -1
                for m in exact_re.finditer(search_text):
                    m_start = m.start()
                    cur_line_offset += search_text.count("\n", prev_pos, m_start)
                    prev_pos = m_start
                    ln = c_start + cur_line_offset
                    if ln > c_end or ln == last_matched_line:
                        continue
                    ls = search_text.rfind("\n", 0, m_start) + 1
                    le = search_text.find("\n", m_start)
                    if le == -1:
                        le = len(search_text)
                    line_text = search_text[ls:le]
                    if session_lower is not None and session_lower not in line_text.lower():
                        last_matched_line = ln
                        continue
                    last_matched_line = ln
                    if len(matches) >= max_matches:
                        truncated = True
                        break
                    matches.append(
                        {
                            "line_number": ln,
                            "text": line_text,
                        }
                    )

            if truncated:
                break

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    return {
        "total_matches": len(matches),
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
        "scanned_chunks": scanned_chunks,
        "matches": matches,
    }
