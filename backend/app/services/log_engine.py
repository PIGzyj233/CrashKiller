"""Streaming 256KB chunked zstd log compression, indexing, and O(log N) / <500ms search."""

from __future__ import annotations

import itertools
import json
import os
import re
import re._constants as _rc
import re._parser as _rp
import time
from bisect import bisect_left
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO

import zstandard as zstd

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


_TYPE_FLAGS = _rp.TYPE_FLAGS
_REPEAT_OPS = frozenset({_rc.MIN_REPEAT, _rc.MAX_REPEAT, _rc.POSSESSIVE_REPEAT})
_SPACE_CATS = frozenset({_rc.CATEGORY_SPACE, _rc.CATEGORY_UNI_SPACE})
_NL_CATS = frozenset(
    {
        _rc.CATEGORY_SPACE,
        _rc.CATEGORY_UNI_SPACE,
        _rc.CATEGORY_NOT_DIGIT,
        _rc.CATEGORY_UNI_NOT_DIGIT,
        _rc.CATEGORY_NOT_WORD,
        _rc.CATEGORY_UNI_NOT_WORD,
        _rc.CATEGORY_LOC_NOT_WORD,
        _rc.CATEGORY_LINEBREAK,
        _rc.CATEGORY_UNI_LINEBREAK,
    }
)
_ASCII_SAFE_CATS = frozenset(
    {
        _rc.CATEGORY_DIGIT,
        _rc.CATEGORY_SPACE,
        _rc.CATEGORY_WORD,
    }
)
_UNICODE_CI_AMBIGUOUS: dict[int, tuple[bytes, ...]] = {
    ord("i"): (b"i", b"\xc4\xb0", b"\xc4\xb1"),
    ord("k"): (b"k", b"\xe2\x84\xaa"),
    ord("s"): (b"s", b"\xc5\xbf"),
}
_UNICODE_CI_AMBIGUOUS_SET = frozenset(
    {
        ord("I"),
        ord("K"),
        ord("S"),
        ord("i"),
        ord("k"),
        ord("s"),
    }
)
_MAX_DNF_BRANCHES = 16


def _combine_flags(flags: int, add_flags: int, del_flags: int) -> int:
    """Combine scoped AST flags matching CPython re._compiler._combine_flags."""
    if add_flags & _TYPE_FLAGS:
        flags &= ~_TYPE_FLAGS
    return (flags | add_flags) & ~del_flags


def _has_multiline_risk(subpattern: _rp.SubPattern, flags: int) -> bool:
    """Return True if subpattern can match '\\n' or has chunk-vs-line anchor differences."""
    for op, av in subpattern.data:
        if op is _rc.LITERAL:
            if av == 10:
                return True
        elif op is _rc.NOT_LITERAL:
            if av != 10:
                return True
        elif op is _rc.ANY:
            if flags & re.DOTALL:
                return True
        elif op is _rc.IN:
            negate = bool(av and av[0][0] is _rc.NEGATE)
            items = av[1:] if negate else av
            if negate:
                excludes_nl = any(
                    (so is _rc.LITERAL and sa == 10)
                    or (so is _rc.RANGE and sa[0] <= 10 <= sa[1])
                    or (so is _rc.CATEGORY and sa in _SPACE_CATS)
                    for so, sa in items
                )
                if not excludes_nl:
                    return True
            else:
                if any(
                    (so is _rc.LITERAL and sa == 10)
                    or (so is _rc.RANGE and sa[0] <= 10 <= sa[1])
                    or (so is _rc.CATEGORY and sa in _NL_CATS)
                    for so, sa in items
                ):
                    return True
        elif op is _rc.AT:
            if av in (
                _rc.AT_BEGINNING_STRING,
                _rc.AT_END_STRING,
                _rc.AT_NON_BOUNDARY,
                _rc.AT_LOC_NON_BOUNDARY,
                _rc.AT_UNI_NON_BOUNDARY,
            ):
                return True
            if av in (_rc.AT_BEGINNING, _rc.AT_END) and not (flags & re.MULTILINE):
                return True
        elif op is _rc.SUBPATTERN:
            _, add_f, del_f, sub_p = av
            if _has_multiline_risk(sub_p, _combine_flags(flags, add_f, del_f)):
                return True
        elif op is _rc.ATOMIC_GROUP:
            if _has_multiline_risk(av, flags):
                return True
        elif op is _rc.BRANCH:
            if any(_has_multiline_risk(b, flags) for b in av[1]):
                return True
        elif op in _REPEAT_OPS:
            if _has_multiline_risk(av[2], flags):
                return True
        elif op in (_rc.ASSERT, _rc.ASSERT_NOT):
            if _has_multiline_risk(av[1], flags):
                return True
        elif op is _rc.GROUPREF_EXISTS:
            if _has_multiline_risk(av[1], flags):
                return True
            if av[2] is not None and _has_multiline_risk(av[2], flags):
                return True
        elif op is _rc.GROUPREF:
            pass
        else:
            return True
    return False


@dataclass(frozen=True, slots=True)
class NeedleSpec:
    """One mandatory byte-level substring check (any variant must appear)."""

    variants: tuple[bytes, ...]
    use_lower: bool


def _tokens_to_needle(tokens: list[tuple[int, bool, bool]]) -> NeedleSpec | None:
    """Convert a contiguous literal token run (cp, ci, unicode_ci) into a NeedleSpec."""
    if not tokens:
        return None

    if all(not ci for _, ci, _ in tokens):
        exact = "".join(chr(cp) for cp, _, _ in tokens).encode("utf-8")
        if len(exact) >= 2:
            return NeedleSpec(variants=(exact,), use_lower=False)
        return None

    # Mixed or case-insensitive run: all tokens must be ASCII
    choices_list: list[tuple[bytes, ...]] = []
    prod = 1
    for cp, ci, unicode_ci in tokens:
        if cp >= 128:
            return None
        low_b = chr(cp).lower().encode("ascii")
        if ci and unicode_ci and low_b[0] in _UNICODE_CI_AMBIGUOUS:
            choices = _UNICODE_CI_AMBIGUOUS[low_b[0]]
        else:
            choices = (low_b,)
        choices_list.append(choices)
        prod *= len(choices)

    if len(tokens) >= 2 and prod <= 3:
        variants = tuple(b"".join(p) for p in itertools.product(*choices_list))
        return NeedleSpec(variants=variants, use_lower=True)

    # Pick longest >=3-char i/k/s-free sub-slice
    best_start = 0
    best_len = 0
    cur_start = 0
    for idx, choices in enumerate(choices_list):
        if len(choices) > 1:
            if idx - cur_start > best_len:
                best_start = cur_start
                best_len = idx - cur_start
            cur_start = idx + 1
    if len(choices_list) - cur_start > best_len:
        best_start = cur_start
        best_len = len(choices_list) - cur_start

    if best_len >= 3:
        needle = b"".join(choices_list[i][0] for i in range(best_start, best_start + best_len))
        return NeedleSpec(variants=(needle,), use_lower=True)

    # Fallback: longest sub-slice (len >= 2) with Cartesian product <= 9
    best_slice: tuple[int, int, int] | None = None  # (length, -prod, start)
    n = len(choices_list)
    for start in range(n):
        cur_prod = 1
        for end in range(start + 1, n + 1):
            cur_prod *= len(choices_list[end - 1])
            if cur_prod > 9:
                break
            length = end - start
            if length >= 2:
                cand = (length, -cur_prod, start)
                if best_slice is None or cand > best_slice:
                    best_slice = cand

    if best_slice is not None:
        length, _, start = best_slice
        variants = tuple(
            b"".join(p) for p in itertools.product(*choices_list[start : start + length])
        )
        return NeedleSpec(variants=variants, use_lower=True)

    return None


def _extract_linear_literals(
    subpattern: _rp.SubPattern, flags: int
) -> list[tuple[int, bool, bool]] | None:
    """Return literal tokens if subpattern is a purely linear literal sequence, else None."""
    out: list[tuple[int, bool, bool]] = []
    ci = bool(flags & re.IGNORECASE)
    unicode_ci = ci and not bool(flags & re.ASCII)
    for op, av in subpattern.data:
        if op is _rc.LITERAL:
            out.append((av, ci, unicode_ci))
        elif op is _rc.SUBPATTERN:
            _, add_f, del_f, sub_p = av
            sub_toks = _extract_linear_literals(sub_p, _combine_flags(flags, add_f, del_f))
            if sub_toks is None:
                return None
            out.extend(sub_toks)
        elif op is _rc.ATOMIC_GROUP:
            sub_toks = _extract_linear_literals(av, flags)
            if sub_toks is None:
                return None
            out.extend(sub_toks)
        elif op in _REPEAT_OPS:
            min_rep, max_rep, item = av
            if min_rep == max_rep and 1 <= min_rep <= 16:
                sub_toks = _extract_linear_literals(item, flags)
                if sub_toks is None:
                    return None
                out.extend(sub_toks * min_rep)
            else:
                return None
        else:
            return None
    return out


def _extract_dnf_needles(
    subpattern: _rp.SubPattern, flags: int
) -> list[tuple[NeedleSpec, ...]] | None:
    """Extract sound DNF of mandatory NeedleSpecs from an AST subpattern."""
    branches: list[tuple[NeedleSpec, ...]] = [()]
    run: list[tuple[int, bool, bool]] = []

    def _flush_run() -> None:
        nonlocal branches
        if not run:
            return
        spec = _tokens_to_needle(run)
        run.clear()
        if spec is not None:
            branches = [b + (spec,) for b in branches]

    def _append_token(cp: int, tok_ci: bool, tok_uci: bool) -> None:
        if tok_ci and cp >= 128:
            _flush_run()
            return
        if cp >= 128:
            if run and any(r_ci for _, r_ci, _ in run):
                _flush_run()
            run.append((cp, False, False))
        else:
            if tok_ci and run and any(r_cp >= 128 for r_cp, _, _ in run):
                _flush_run()
            run.append((cp, tok_ci, tok_uci))

    def _merge_sub(sub_dnf: list[tuple[NeedleSpec, ...]] | None) -> None:
        nonlocal branches
        _flush_run()
        if not sub_dnf:
            return
        if len(branches) * len(sub_dnf) > _MAX_DNF_BRANCHES:
            return
        branches = [b + sb for b in branches for sb in sub_dnf]

    def _merge_alternates(alt_list: list[_rp.SubPattern], alt_flags: int) -> None:
        _flush_run()
        if len(alt_list) > _MAX_DNF_BRANCHES:
            return
        combined: list[tuple[NeedleSpec, ...]] = []
        for alt in alt_list:
            res = _extract_dnf_needles(alt, alt_flags)
            if not res:
                return
            combined.extend(res)
            if len(combined) > _MAX_DNF_BRANCHES:
                return
        _merge_sub(combined)

    for op, av in subpattern.data:
        ci = bool(flags & re.IGNORECASE)
        unicode_ci = ci and not bool(flags & re.ASCII)

        if op is _rc.LITERAL:
            _append_token(av, ci, unicode_ci)
        elif op is _rc.SUBPATTERN:
            _, add_f, del_f, sub_p = av
            sub_flags = _combine_flags(flags, add_f, del_f)
            linear = _extract_linear_literals(sub_p, sub_flags)
            if linear is not None:
                for cp, t_ci, t_uci in linear:
                    _append_token(cp, t_ci, t_uci)
            else:
                _merge_sub(_extract_dnf_needles(sub_p, sub_flags))
        elif op is _rc.ATOMIC_GROUP:
            linear = _extract_linear_literals(av, flags)
            if linear is not None:
                for cp, t_ci, t_uci in linear:
                    _append_token(cp, t_ci, t_uci)
            else:
                _merge_sub(_extract_dnf_needles(av, flags))
        elif op in _REPEAT_OPS:
            min_rep, max_rep, item = av
            if min_rep == 0:
                _flush_run()
            elif min_rep == max_rep and 1 <= min_rep <= 16:
                linear = _extract_linear_literals(item, flags)
                if linear is not None:
                    for cp, t_ci, t_uci in linear * min_rep:
                        _append_token(cp, t_ci, t_uci)
                else:
                    _merge_sub(_extract_dnf_needles(item, flags))
            else:
                # min_rep >= 1, variable upper bound: flush around item so a+b != "ab" falsely
                _flush_run()
                _merge_sub(_extract_dnf_needles(item, flags))
                _flush_run()
        elif op is _rc.BRANCH:
            _merge_alternates(av[1], flags)
        elif op is _rc.GROUPREF_EXISTS:
            _, item_yes, item_no = av
            if item_no is None:
                _flush_run()
            else:
                _merge_alternates([item_yes, item_no], flags)
        elif op is _rc.ASSERT:
            _flush_run()
            _merge_sub(_extract_dnf_needles(av[1], flags))
        else:
            _flush_run()

    _flush_run()
    if any(not b for b in branches):
        return None
    return branches


def _is_bytes_safe_op(op: Any, av: Any, flags: int) -> bool:
    """Return True if an AST node has identical ASCII bytes-regex precheck semantics."""
    ci = bool(flags & re.IGNORECASE)
    unicode_ci = ci and not bool(flags & re.ASCII)
    is_ascii = bool(flags & re.ASCII)

    if op is _rc.LITERAL:
        if not (0 <= av <= 127):
            return False
        if unicode_ci and av in _UNICODE_CI_AMBIGUOUS_SET:
            return False
        return True
    if op is _rc.IN:
        if not av or av[0][0] is _rc.NEGATE:
            return False
        for so, sa in av:
            if so is _rc.LITERAL:
                if not (0 <= sa <= 127):
                    return False
                if unicode_ci and sa in _UNICODE_CI_AMBIGUOUS_SET:
                    return False
            elif so is _rc.RANGE:
                lo, hi = sa
                if lo < 0 or hi > 127:
                    return False
                if unicode_ci and any(c in _UNICODE_CI_AMBIGUOUS_SET for c in range(lo, hi + 1)):
                    return False
            elif so is _rc.CATEGORY:
                if not (is_ascii and sa in _ASCII_SAFE_CATS):
                    return False
            else:
                return False
        return True
    if op is _rc.AT:
        if av in (_rc.AT_BEGINNING, _rc.AT_END):
            return bool(flags & re.MULTILINE)
        if av in (_rc.AT_BOUNDARY, _rc.AT_LOC_BOUNDARY, _rc.AT_UNI_BOUNDARY):
            return is_ascii
        return False
    if op is _rc.SUBPATTERN:
        _, add_f, del_f, sub_p = av
        return _is_bytes_safe_subpattern(sub_p, _combine_flags(flags, add_f, del_f))
    if op is _rc.ATOMIC_GROUP:
        return _is_bytes_safe_subpattern(av, flags)
    if op is _rc.BRANCH:
        return all(_is_bytes_safe_subpattern(b, flags) for b in av[1])
    if op in _REPEAT_OPS:
        return _is_bytes_safe_subpattern(av[2], flags)
    if op is _rc.ASSERT:
        return _is_bytes_safe_subpattern(av[1], flags)
    return False


def _is_bytes_safe_subpattern(subpattern: _rp.SubPattern, flags: int) -> bool:
    return all(_is_bytes_safe_op(op, av, flags) for op, av in subpattern.data)


def _needs_ci_lowering(subpattern: _rp.SubPattern, flags: int) -> bool:
    """Return True if any bytes-safe op in subpattern relies on IGNORECASE on ASCII letters."""
    ci = bool(flags & re.IGNORECASE)
    for op, av in subpattern.data:
        if op is _rc.LITERAL:
            if ci and (65 <= av <= 90 or 97 <= av <= 122):
                return True
        elif op is _rc.IN:
            if ci:
                for so, sa in av:
                    if so is _rc.LITERAL and (65 <= sa <= 90 or 97 <= sa <= 122):
                        return True
                    if so is _rc.RANGE:
                        lo, hi = sa
                        if (lo <= 90 and hi >= 65) or (lo <= 122 and hi >= 97):
                            return True
        elif op is _rc.SUBPATTERN:
            _, add_f, del_f, sub_p = av
            if _needs_ci_lowering(sub_p, _combine_flags(flags, add_f, del_f)):
                return True
        elif op is _rc.ATOMIC_GROUP:
            if _needs_ci_lowering(av, flags):
                return True
        elif op is _rc.BRANCH:
            if any(_needs_ci_lowering(b, flags) for b in av[1]):
                return True
        elif op in _REPEAT_OPS:
            if _needs_ci_lowering(av[2], flags):
                return True
        elif op is _rc.ASSERT:
            if _needs_ci_lowering(av[1], flags):
                return True
    return False


def _escape_cc_byte(b: int) -> bytes:
    ch = bytes((b,))
    if ch in b"\\]-^[":
        return b"\\" + ch
    if 0x20 <= b <= 0x7E:
        return ch
    return f"\\x{b:02x}".encode("ascii")


def _escape_lit_byte(b: int) -> bytes:
    ch = bytes((b,))
    if ch in b"\\.^$*+?{}[]()|":
        return b"\\" + ch
    if 0x20 <= b <= 0x7E:
        return ch
    return f"\\x{b:02x}".encode("ascii")


def _ast_to_bytes_regex(
    subpattern: _rp.SubPattern,
    flags: int,
    lower_ci: bool,
) -> bytes | None:
    """Serialize a bytes-safe AST subpattern into an ASCII bytes regex."""
    out: list[bytes] = []
    for op, av in subpattern.data:
        if op is _rc.LITERAL:
            b = av + 32 if (lower_ci and 65 <= av <= 90) else av
            out.append(_escape_lit_byte(b))
        elif op is _rc.IN:
            cc: list[bytes] = [b"["]
            for so, sa in av:
                if so is _rc.LITERAL:
                    b = sa + 32 if (lower_ci and 65 <= sa <= 90) else sa
                    cc.append(_escape_cc_byte(b))
                elif so is _rc.RANGE:
                    lo, hi = sa
                    if lower_ci:
                        if 65 <= lo <= hi <= 90:
                            lo += 32
                            hi += 32
                        elif lo <= 90 and hi >= 65:
                            return None
                    cc.append(_escape_cc_byte(lo) + b"-" + _escape_cc_byte(hi))
                elif so is _rc.CATEGORY:
                    if sa is _rc.CATEGORY_DIGIT:
                        cc.append(b"\\d")
                    elif sa is _rc.CATEGORY_SPACE:
                        cc.append(b"\\s")
                    elif sa is _rc.CATEGORY_WORD:
                        cc.append(b"\\w")
                    else:
                        return None
                else:
                    return None
            cc.append(b"]")
            out.append(b"".join(cc))
        elif op is _rc.AT:
            if av is _rc.AT_BEGINNING:
                out.append(b"^")
            elif av is _rc.AT_END:
                out.append(b"$")
            elif av in (_rc.AT_BOUNDARY, _rc.AT_LOC_BOUNDARY, _rc.AT_UNI_BOUNDARY):
                out.append(b"\\b")
            else:
                return None
        elif op is _rc.SUBPATTERN:
            _, add_f, del_f, sub_p = av
            inner = _ast_to_bytes_regex(sub_p, _combine_flags(flags, add_f, del_f), lower_ci)
            if inner is None:
                return None
            out.append(b"(?:" + inner + b")")
        elif op is _rc.ATOMIC_GROUP:
            inner = _ast_to_bytes_regex(av, flags, lower_ci)
            if inner is None:
                return None
            out.append(b"(?>" + inner + b")")
        elif op is _rc.BRANCH:
            parts: list[bytes] = []
            for b in av[1]:
                part = _ast_to_bytes_regex(b, flags, lower_ci)
                if part is None:
                    return None
                parts.append(part)
            out.append(b"|".join(parts))
        elif op in _REPEAT_OPS:
            min_rep, max_rep, item = av
            inner = _ast_to_bytes_regex(item, flags, lower_ci)
            if inner is None:
                return None
            if len(item.data) != 1 or item.data[0][0] not in (_rc.LITERAL, _rc.IN):
                inner = b"(?:" + inner + b")"
            if min_rep == 0 and max_rep == _rc.MAXREPEAT:
                q = b"*"
            elif min_rep == 1 and max_rep == _rc.MAXREPEAT:
                q = b"+"
            elif min_rep == 0 and max_rep == 1:
                q = b"?"
            elif min_rep == max_rep:
                q = f"{{{min_rep}}}".encode("ascii")
            elif max_rep == _rc.MAXREPEAT:
                q = f"{{{min_rep},}}".encode("ascii")
            else:
                q = f"{{{min_rep},{max_rep}}}".encode("ascii")
            if op is _rc.MIN_REPEAT:
                q += b"?"
            elif op is _rc.POSSESSIVE_REPEAT:
                q += b"+"
            out.append(inner + q)
        elif op is _rc.ASSERT:
            direction, sub_p = av
            inner = _ast_to_bytes_regex(sub_p, flags, lower_ci)
            if inner is None:
                return None
            out.append((b"(?=" if direction > 0 else b"(?<=") + inner + b")")
        else:
            return None
    return b"".join(out)


def _extract_anchored_bytes_regex(
    subpattern: _rp.SubPattern,
    flags: int,
) -> tuple[bytes, bool] | None:
    """Compile a sound bytes-regex (preferring literal-anchored suffix for SRE_INFO_PREFIX)."""
    data = subpattern.data
    # 1. Try starting from the first bytes-safe top-level LITERAL followed by >=1 safe ops
    for idx, (op, av) in enumerate(data):
        if op is _rc.LITERAL and _is_bytes_safe_op(op, av, flags):
            safe_end = idx + 1
            while safe_end < len(data) and _is_bytes_safe_op(
                data[safe_end][0], data[safe_end][1], flags
            ):
                safe_end += 1
            if safe_end - idx >= 2:
                slice_sp = _rp.SubPattern(subpattern.state, data[idx:safe_end])
                lower_ci = _needs_ci_lowering(slice_sp, flags)
                bytes_pat = _ast_to_bytes_regex(slice_sp, flags, lower_ci)
                if bytes_pat is not None:
                    return bytes_pat, lower_ci

    # 2. Fallback to compiling the entire subpattern if completely bytes-safe
    if data and _is_bytes_safe_subpattern(subpattern, flags):
        lower_ci = _needs_ci_lowering(subpattern, flags)
        bytes_pat = _ast_to_bytes_regex(subpattern, flags, lower_ci)
        if bytes_pat is not None:
            return bytes_pat, lower_ci

    return None


@dataclass(frozen=True, slots=True)
class SearchPlan:
    """Compiled two-stage search plan for LogEngine.search()."""

    exact_re: re.Pattern[str]
    precheck_mode: str
    multiline_risk: bool
    needle_bytes: bytes = b""
    dnf_branches: tuple[tuple[NeedleSpec, ...], ...] = ()
    bytes_re: re.Pattern[bytes] | None = None


def compile_search_plan(
    pattern: str,
    is_regex: bool,
    case_sensitive: bool,
) -> SearchPlan:
    """Compile exact regex and sound Stage-1 prefilter + Stage-2 multiline plan."""
    re_flags = re.MULTILINE | (0 if case_sensitive else re.IGNORECASE)

    if not is_regex:
        exact_re = re.compile(re.escape(pattern), re_flags)
        multiline_risk = "\n" in pattern
        if case_sensitive:
            return SearchPlan(
                exact_re=exact_re,
                precheck_mode="literal_exact",
                multiline_risk=multiline_risk,
                needle_bytes=pattern.encode("utf-8"),
            )

        # Case-insensitive literal: split at non-ASCII chars and process each ASCII segment
        specs: list[NeedleSpec] = []
        seg: list[tuple[int, bool, bool]] = []
        for ch in pattern:
            cp = ord(ch)
            if cp >= 128:
                if seg:
                    sp = _tokens_to_needle(seg)
                    if sp is not None:
                        specs.append(sp)
                    seg.clear()
            else:
                seg.append((cp, True, True))
        if seg:
            sp = _tokens_to_needle(seg)
            if sp is not None:
                specs.append(sp)

        if specs:
            if len(specs) == 1 and len(specs[0].variants) == 1 and specs[0].use_lower:
                return SearchPlan(
                    exact_re=exact_re,
                    precheck_mode="literal_lower",
                    multiline_risk=multiline_risk,
                    needle_bytes=specs[0].variants[0],
                )
            return SearchPlan(
                exact_re=exact_re,
                precheck_mode="literals_dnf",
                multiline_risk=multiline_risk,
                dnf_branches=(tuple(specs),),
            )

        if (
            len(pattern) == 1
            and ord(pattern) < 128
            and ord(pattern) not in _UNICODE_CI_AMBIGUOUS_SET
        ):
            return SearchPlan(
                exact_re=exact_re,
                precheck_mode="literal_lower",
                multiline_risk=multiline_risk,
                needle_bytes=pattern.lower().encode("ascii"),
            )

        return SearchPlan(
            exact_re=exact_re,
            precheck_mode="none",
            multiline_risk=multiline_risk,
        )

    try:
        exact_re = re.compile(pattern, re_flags)
    except re.error as exc:
        raise ValueError(f"Invalid regex pattern: {exc}") from exc

    try:
        parsed = _rp.parse(pattern, re_flags)
        base_flags = parsed.state.flags | re_flags
        multiline_risk = _has_multiline_risk(parsed, base_flags)

        dnf = _extract_dnf_needles(parsed, base_flags)
        if dnf:
            return SearchPlan(
                exact_re=exact_re,
                precheck_mode="literals_dnf",
                multiline_risk=multiline_risk,
                dnf_branches=tuple(dnf),
            )

        bytes_spec = _extract_anchored_bytes_regex(parsed, base_flags)
        if bytes_spec is not None:
            bytes_pat, lower_ci = bytes_spec
            bytes_re = re.compile(bytes_pat, re.MULTILINE)
            return SearchPlan(
                exact_re=exact_re,
                precheck_mode="regex_bytes_lower" if lower_ci else "regex_bytes",
                multiline_risk=multiline_risk,
                bytes_re=bytes_re,
            )

        return SearchPlan(
            exact_re=exact_re,
            precheck_mode="none",
            multiline_risk=multiline_risk,
        )
    except Exception:
        return SearchPlan(
            exact_re=exact_re,
            precheck_mode="none",
            multiline_risk=True,
        )


class LogEngine:
    """Chunked seekable zstd log storage, indexing, random access, and fast regex search."""

    def ingest_log(
        self,
        input_path_or_stream: Path | str | BinaryIO,
        zst_path: Path | str,
        idx_path: Path | str,
        chunk_size_bytes: int = 262144,
        zstd_level: int = 6,
    ) -> dict[str, Any]:
        """Stream-ingest a raw log into 256KB newline-aligned zstd frames + .idx.json."""
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

        def _flush_chunk(zst_fh: BinaryIO) -> None:
            nonlocal compressed_size
            nonlocal chunk_start_line, chunk_first_ts, chunk_last_ts
            nonlocal chunk_min_dt, chunk_min_ts, chunk_max_dt, chunk_max_ts
            nonlocal chunk_error_count, chunk_warn_count
            if not chunk_buf:
                return
            frame = compressor.compress(bytes(chunk_buf))
            byte_offset = compressed_size
            zst_fh.write(frame)
            frame_len = len(frame)
            compressed_size += frame_len
            chunks_meta.append(
                {
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
            )
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

        def _process_raw_line(raw_line: bytes, zst_fh: BinaryIO) -> None:
            nonlocal total_lines, chunk_first_ts, chunk_last_ts
            nonlocal chunk_min_dt, chunk_min_ts, chunk_max_dt, chunk_max_ts
            nonlocal chunk_error_count, chunk_warn_count

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

            if level in ERROR_LEVELS:
                chunk_error_count += 1
                recent_errors.append(
                    {
                        "line_number": total_lines,
                        "text": text,
                        "timestamp": ts_iso,
                        "level": level,
                    }
                )
            elif level in WARN_LEVELS:
                chunk_warn_count += 1

            tail_lines.append(
                {
                    "line_number": total_lines,
                    "text": text,
                    "timestamp": ts_iso,
                    "level": level,
                }
            )

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
            with open(zst_tmp, "wb") as zst_fh:
                pending = bytearray()
                while True:
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
        except Exception:
            if zst_tmp.exists():
                zst_tmp.unlink()
            raise
        finally:
            if close_src:
                src_fh.close()

        digest_json = {
            "total_lines": total_lines,
            "error_count": sum(c["error_count"] for c in chunks_meta),
            "warn_count": sum(c["warn_count"] for c in chunks_meta),
            "tail_lines": list(tail_lines),
            "recent_errors": list(recent_errors),
        }
        idx_data = {
            "version": 1,
            "chunk_size_bytes": chunk_size_bytes,
            "raw_size": raw_size,
            "compressed_size": compressed_size,
            "total_lines": total_lines,
            "chunks": chunks_meta,
            "digest": digest_json,
        }

        try:
            with open(idx_tmp, "w", encoding="utf-8") as idx_fh:
                json.dump(idx_data, idx_fh, ensure_ascii=False)
                idx_fh.flush()
                os.fsync(idx_fh.fileno())
            os.replace(zst_tmp, zst_dest)
            os.replace(idx_tmp, idx_dest)
        except Exception:
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

    def _load_idx(self, idx_path: Path | str) -> dict[str, Any]:
        with open(idx_path, encoding="utf-8") as fh:
            return json.load(fh)

    def read_lines(
        self,
        zst_path: Path | str,
        idx_path: Path | str,
        start_line: int,
        end_line: int,
    ) -> list[dict[str, Any]]:
        """Read inclusive [start_line, end_line] using O(log N) chunk binary search."""
        idx_data = self._load_idx(idx_path)
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

    def read_around_time(
        self,
        zst_path: Path | str,
        idx_path: Path | str,
        target_time: datetime | str,
        window_seconds: int = 10,
        max_lines: int = 500,
    ) -> list[dict[str, Any]]:
        """Read log lines within [target_time - window_seconds, target_time + window_seconds]."""
        if isinstance(target_time, str):
            target_dt = _parse_ts(target_time)
        elif target_time.tzinfo is None:
            target_dt = target_time.replace(tzinfo=UTC)
        else:
            target_dt = target_time.astimezone(UTC)
        if target_dt is None:
            raise ValueError(f"Invalid target_time: {target_time!r}")

        idx_data = self._load_idx(idx_path)
        chunks: list[dict[str, Any]] = idx_data.get("chunks", [])
        if not chunks:
            return []

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
                if prev_idx is not None and idx != prev_idx + 1:
                    current_ts = None
                prev_idx = idx
                chunk = chunks[idx]
                c_start = int(chunk["start_line"])
                zst_fh.seek(int(chunk["byte_offset"]))
                comp_bytes = zst_fh.read(int(chunk["compressed_size"]))
                raw_text = decompressor.decompress(comp_bytes).decode("utf-8")

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

    def search(
        self,
        zst_path: Path | str,
        idx_path: Path | str,
        pattern: str,
        is_regex: bool = False,
        max_matches: int = 100,
        case_sensitive: bool = False,
    ) -> dict[str, Any]:
        """Stream-search compressed log chunks using two-stage bytes prefiltering."""
        if not pattern:
            raise ValueError("Search pattern must not be empty")
        if max_matches <= 0:
            raise ValueError("max_matches must be positive")

        t0 = time.perf_counter()
        idx_data = self._load_idx(idx_path)
        chunks: list[dict[str, Any]] = idx_data.get("chunks", [])

        if not chunks:
            return {
                "total_matches": 0,
                "truncated": False,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                "scanned_chunks": 0,
                "matches": [],
            }

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
                scanned_chunks += 1
                zst_fh.seek(int(chunk["byte_offset"]))
                comp_bytes = zst_fh.read(int(chunk["compressed_size"]))
                chunk_bytes = decompressor.decompress(comp_bytes)

                # Stage 1: fast C-speed bytes prefilter
                if precheck_mode == "literal_exact":
                    if plan.needle_bytes not in chunk_bytes:
                        continue
                elif precheck_mode == "literal_lower":
                    if plan.needle_bytes not in chunk_bytes.lower():
                        continue
                elif precheck_mode == "literals_dnf":
                    low_bytes: bytes | None = None
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
                    if not plan.bytes_re.search(chunk_bytes.lower()):
                        continue

                # Stage 2: exact UTF-8 line matching on hit chunks only
                text = chunk_bytes.decode("utf-8")
                c_start = int(chunk["start_line"])
                c_end = int(chunk["end_line"])

                if multiline_risk:
                    for offset, line_text in enumerate(_split_chunk_lines(text)):
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
                        last_matched_line = ln
                        if len(matches) >= max_matches:
                            truncated = True
                            break
                        ls = search_text.rfind("\n", 0, m_start) + 1
                        le = search_text.find("\n", m_start)
                        if le == -1:
                            le = len(search_text)
                        matches.append(
                            {
                                "line_number": ln,
                                "text": search_text[ls:le],
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


__all__ = ["LogEngine"]
