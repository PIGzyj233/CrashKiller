"""Two-stage search plan compilation and bytes-regex emission."""

from __future__ import annotations

import re
import re._constants as _rc
import re._parser as _rp
from dataclasses import dataclass

from app.services.log_engine.prefilter_ast import (
    _REPEAT_OPS,
    _UNICODE_CI_AMBIGUOUS_SET,
    NeedleSpec,
    _combine_flags,
    _extract_dnf_needles,
    _has_multiline_risk,
    _is_bytes_safe_op,
    _is_bytes_safe_subpattern,
    _needs_ci_lowering,
    _tokens_to_needle,
)


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
