"""Timestamp, log level, line encoding, and session ID parsing helpers."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

TIMESTAMP_RE = re.compile(
    r"(\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:[.,]\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})?)"
)
LEVEL_RE = re.compile(
    r"\b(FATAL|ASSERT|ERROR|WARNING|WARN|INFO|DEBUG|TRACE)\b",
    re.IGNORECASE,
)
ERROR_LEVELS = frozenset({"ERROR", "FATAL", "ASSERT"})
WARN_LEVELS = frozenset({"WARN", "WARNING"})

MAX_SESSION_ID_LENGTH = 128
POST_SESSION_CONTEXT_LINES = 30

_FAST_TS_RE = re.compile(
    r"^(\d{4})[-/](\d{2})[-/](\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
    r"(?:[.,](\d{1,9}))?(Z|[+-]\d{2}:?\d{2})?$"
)


def _parse_ts(ts_str: str) -> datetime | None:
    """Parse common log timestamp strings into UTC-aware datetime."""
    m = _FAST_TS_RE.match(ts_str.strip())
    if m:
        yr, mo, dy, hr, mn, sc, frac, tz_str = m.groups()
        micro = int((frac + "000000")[:6]) if frac else 0
        try:
            tz = UTC
            if tz_str and tz_str != "Z":
                clean_tz = tz_str.replace(":", "")
                sign = 1 if clean_tz[0] == "+" else -1
                off_h = int(clean_tz[1:3])
                off_m = int(clean_tz[3:5])
                tz = timezone(timedelta(hours=sign * off_h, minutes=sign * off_m))
            dt = datetime(int(yr), int(mo), int(dy), int(hr), int(mn), int(sc), micro, tzinfo=tz)
            return dt.astimezone(UTC) if tz is not UTC else dt
        except (ValueError, OverflowError):
            return None

    normalized = ts_str.strip().replace("/", "-").replace(",", ".")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _decode_line(raw_line: bytes) -> str:
    """Decode a single raw log line with UTF-8 -> GB18030 -> UTF-8 replace fallback."""
    try:
        return raw_line.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw_line.decode("gb18030")
        except UnicodeDecodeError:
            return raw_line.decode("utf-8", errors="replace")


def _extract_line_meta_with_dt(line: str) -> tuple[str | None, datetime | None, str | None]:
    """Extract normalized UTC ISO timestamp, parsed datetime, and log level from a line."""
    ts_iso: str | None = None
    ts_dt: datetime | None = None
    ts_m = TIMESTAMP_RE.search(line[:64])
    if ts_m:
        dt = _parse_ts(ts_m.group(1))
        if dt is not None:
            ts_dt = dt
            ts_iso = dt.isoformat()

    level: str | None = None
    lvl_m = LEVEL_RE.search(line[:128] if len(line) > 128 else line)
    if lvl_m:
        lvl = lvl_m.group(1).upper()
        level = "WARN" if lvl == "WARNING" else lvl

    return ts_iso, ts_dt, level


def _extract_line_meta(line: str) -> tuple[str | None, str | None]:
    """Extract normalized UTC ISO timestamp and log level from a log line."""
    ts_iso, _, level = _extract_line_meta_with_dt(line)
    return ts_iso, level


def _split_chunk_lines(raw_text: str) -> list[str]:
    """Split chunk text strictly on '\\n' to match ingest_log line boundaries."""
    if not raw_text:
        return []
    return (raw_text[:-1] if raw_text.endswith("\n") else raw_text).split("\n")


def normalize_session_id(raw: str | None) -> str | None:
    """Validate and normalize an optional custom session_id."""
    if raw is None:
        return None
    if any(ch in raw for ch in ("\r", "\n", "\x00")):
        raise ValueError("session_id must not contain control characters")
    cleaned = raw.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_SESSION_ID_LENGTH:
        raise ValueError(f"session_id must not exceed {MAX_SESSION_ID_LENGTH} characters")
    return cleaned


def _session_ascii_needle(session_lower: str) -> bytes | None:
    """Return lowercase UTF-8 bytes needle for sound C-speed chunk prefiltering when ASCII."""
    return session_lower.encode("utf-8") if session_lower.isascii() else None


def _index_matches_session(idx_data: dict[str, Any], session_lower: str) -> bool:
    raw = idx_data.get("session_id")
    return isinstance(raw, str) and raw.lower() == session_lower
