"""Regex AST analysis for sound Stage-1 bytes prefiltering."""

from __future__ import annotations

import itertools
import re
import re._constants as _rc
import re._parser as _rp
from dataclasses import dataclass
from typing import Any

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
