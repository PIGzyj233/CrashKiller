"""Version parsing and comparison helpers for regression detection."""

from __future__ import annotations

import re

_VERSION_NUM_RE = re.compile(r"\d+")
_PRERELEASE_TOKEN_RE = re.compile(r"\d+|[a-zA-Z]+")
_INLINE_PRE_RE = re.compile(r"^([\d.]+?)([a-zA-Z].*)$")


def _parse_version(
    version: str,
) -> tuple[list[int], int, list[tuple[int, int | str]]] | None:
    s = (version or "").strip()
    if not s:
        return None
    if s[0] in ("v", "V") and len(s) > 1 and s[1].isdigit():
        s = s[1:]
    # Strip SemVer build metadata (+...) before comparing precedence.
    s = s.split("+", 1)[0].strip()
    if "-" in s:
        core_part, pre_part = s.split("-", 1)
    else:
        core_part, pre_part = s, ""
        inline_match = _INLINE_PRE_RE.match(core_part)
        if inline_match:
            core_part, pre_part = inline_match.group(1), inline_match.group(2)

    core_nums = [int(x) for x in _VERSION_NUM_RE.findall(core_part)]
    if not core_nums:
        return None

    if not pre_part.strip():
        return (core_nums, 1, [])

    pre_tokens: list[tuple[int, int | str]] = []
    for tok in _PRERELEASE_TOKEN_RE.findall(pre_part):
        if tok.isdigit():
            pre_tokens.append((0, int(tok)))
        else:
            pre_tokens.append((1, tok.lower()))
    return (core_nums, 0, pre_tokens)


def _compare_versions(v1: str, v2: str) -> int:
    p1 = _parse_version(v1)
    p2 = _parse_version(v2)
    if p1 is None or p2 is None:
        s1 = (v1 or "").strip()
        s2 = (v2 or "").strip()
        return (s1 > s2) - (s1 < s2)
    cores1, rel1, pre1 = p1
    cores2, rel2, pre2 = p2
    max_len = max(len(cores1), len(cores2))
    padded1 = cores1 + [0] * (max_len - len(cores1))
    padded2 = cores2 + [0] * (max_len - len(cores2))
    k1 = (padded1, rel1, pre1)
    k2 = (padded2, rel2, pre2)
    return (k1 > k2) - (k1 < k2)


def _is_post_fix_version(app_version: str | None, fixed_in_version: str | None) -> bool:
    if not app_version or not fixed_in_version:
        return False
    return _compare_versions(app_version, fixed_in_version) >= 0
