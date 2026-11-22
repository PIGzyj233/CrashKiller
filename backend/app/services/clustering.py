"""Deterministic crash stack filtering, clustering, fuzzy recall, and human KB feedback."""

from __future__ import annotations

import fnmatch
import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import CrashCluster, CrashReport, Issue, Project
from app.services.settings_service import (
    DEFAULT_NOISE_FUNCTIONS,
    DEFAULT_NOISE_MODULES,
)

__all__ = [
    "ClusteringError",
    "ClusteringService",
    "ClusterResult",
    "DEFAULT_NOISE_FUNCTIONS",
    "DEFAULT_NOISE_MODULES",
    "NoiseRules",
    "apply_human_verdict",
    "cluster_crash",
    "compute_signature_hash",
    "filter_and_normalize_frames",
    "normalize_exception_category",
    "normalize_function_name",
    "recall_similar_issues",
    "resolve_noise_rules",
]


class ClusteringError(Exception):
    """Raised on invalid clustering configuration, triage mode, or human verdict input."""


@dataclass(slots=True, frozen=True)
class NoiseRules:
    """Resolved module and function noise filter patterns."""

    noise_modules: frozenset[str]
    noise_functions: tuple[str, ...]


@dataclass(slots=True)
class ClusterResult:
    """Outcome of clustering a CrashReport and evaluating AI triage eligibility."""

    cluster: CrashCluster
    issue: Issue | None
    signature_hash: str
    signature_text: str
    normalized_frames: list[dict[str, Any]]
    is_new_cluster: bool
    should_run_ai: bool
    trigger_reason: str
    similar_issues: list[dict[str, Any]]

    @property
    def should_trigger_ai(self) -> bool:
        return self.should_run_ai


_OFFSET_TAIL_RE = re.compile(r"(?:\s*[+@]\s*(?:0x[0-9a-fA-F]+|\d+))+$")
_LAMBDA_HASH_RE = re.compile(r"<lambda_[0-9a-fA-F]+>|lambda_[0-9a-fA-F]{6,}")
_HEX_ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]{4,}\b")
_MULTISPACE_RE = re.compile(r"\s+")
_TRGM_TOKEN_RE = re.compile(r"[0-9a-zA-Z]+")
_VERSION_NUM_RE = re.compile(r"\d+")
_PRERELEASE_TOKEN_RE = re.compile(r"\d+|[a-zA-Z]+")
_INLINE_PRE_RE = re.compile(r"^([\d.]+?)([a-zA-Z].*)$")
_EXC_ADDR_SUFFIX_RE = re.compile(r"\s*(?:/|@)\s*0x[0-9a-fA-F]+\s*$")

VALID_ISSUE_STATUSES: frozenset[str] = frozenset(
    {"NEW", "KNOWN", "FIXED", "WONTFIX", "REGRESSION"}
)

_TEMPLATE_INNER_OPS: tuple[str, ...] = (
    "<=>",
    "<<=",
    ">>=",
    "->*",
    "<<",
    "<=",
    ">=",
    "->",
)

_OPERATOR_TOKENS: tuple[str, ...] = (
    "<=>",
    "<<=",
    ">>=",
    "->*",
    "<<",
    ">>",
    "<=",
    ">=",
    "->",
    "<",
    ">",
)


def _as_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return utcnow()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _cross_platform_basename(path: str | None) -> str:
    if not path:
        return ""
    cleaned = str(path).strip().replace("\\", "/").rstrip("/")
    if not cleaned:
        return ""
    return cleaned.rsplit("/", 1)[-1].strip().lower()


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


def normalize_exception_category(crash_reason: str | None) -> str:
    """Normalize crash reason / exception type by removing volatile address suffixes."""
    if not crash_reason or not str(crash_reason).strip():
        return "UNKNOWN"
    cleaned = _EXC_ADDR_SUFFIX_RE.sub("", str(crash_reason).strip())
    cleaned = _HEX_ADDR_RE.sub("<addr>", cleaned)
    return cleaned.strip().upper() or "UNKNOWN"


def _is_template_less_than_comparison(text: str, pos: int) -> bool:
    """Return True when `<` at `pos` inside a template list is a `<` comparison, not an opener."""
    n = len(text)
    if (
        pos > 0
        and text[pos - 1].isspace()
        and pos + 1 < n
        and text[pos + 1].isspace()
    ):
        return True

    k = pos - 1
    while k >= 0 and text[k].isspace():
        k -= 1
    if k < 0:
        return True
    ch = text[k]
    if not (ch.isalnum() or ch == "_"):
        return True

    tok_start = k
    while tok_start > 0 and (text[tok_start - 1].isalnum() or text[tok_start - 1] == "_"):
        tok_start -= 1
    return text[tok_start].isdigit()


def _has_unmatched_closer_ahead(text: str, pos: int, sim_depth: int) -> bool:
    """Return True if treating a `>`/`>>` at `pos` as a template closer leaves an unmatched `>`."""
    n = len(text)
    j = pos
    p_depth = 0
    while j < n:
        if text.startswith("__LAMBDA__", j):
            j += len("__LAMBDA__")
            continue
        if text.startswith("<addr>", j):
            j += len("<addr>")
            continue
        if text.startswith("<unknown>", j):
            j += len("<unknown>")
            continue
        if text.startswith("operator", j):
            prev_ok = j == 0 or not (text[j - 1].isalnum() or text[j - 1] == "_")
            if prev_ok:
                rest = text[j + len("operator") :]
                matched_op = next((op for op in _OPERATOR_TOKENS if rest.startswith(op)), None)
                if matched_op is not None:
                    j += len("operator") + len(matched_op)
                    continue

        c = text[j]
        if c in "([":
            p_depth += 1
            j += 1
            continue
        if c in ")]" and p_depth > 0:
            p_depth -= 1
            j += 1
            continue

        if p_depth == 0:
            matched_inner = next((op for op in _TEMPLATE_INNER_OPS if text.startswith(op, j)), None)
            if matched_inner is not None:
                j += len(matched_inner)
                continue
            if c == "<":
                if not _is_template_less_than_comparison(text, j):
                    sim_depth += 1
            elif c == ">":
                sim_depth -= 1
                if sim_depth < 0:
                    return True
        j += 1
    return False


def _collapse_templates(text: str) -> str:
    """Collapse balanced C++ template parameter lists <...> -> <> while preserving operators."""
    out: list[str] = []
    n = len(text)
    i = 0
    depth = 0
    paren_depth = 0

    while i < n:
        if text.startswith("__LAMBDA__", i):
            if depth == 0:
                out.append("__LAMBDA__")
            i += len("__LAMBDA__")
            continue

        if text.startswith("<addr>", i):
            if depth == 0:
                out.append("<addr>")
            i += len("<addr>")
            continue

        if text.startswith("<unknown>", i):
            if depth == 0:
                out.append("<unknown>")
            i += len("<unknown>")
            continue

        if text.startswith("operator", i):
            prev_ok = i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")
            if prev_ok:
                rest = text[i + len("operator") :]
                matched_op = next((op for op in _OPERATOR_TOKENS if rest.startswith(op)), None)
                if matched_op is not None:
                    token = "operator" + matched_op
                    if depth == 0:
                        out.append(token)
                    i += len(token)
                    continue

        ch = text[i]
        if depth == 0:
            if ch == "<":
                out.append("<>")
                depth = 1
                paren_depth = 0
            else:
                out.append(ch)
            i += 1
        else:
            if ch in "([":
                paren_depth += 1
                i += 1
                continue
            if ch in ")]" and paren_depth > 0:
                paren_depth -= 1
                i += 1
                continue

            if paren_depth > 0:
                i += 1
                continue

            matched_inner = next((op for op in _TEMPLATE_INNER_OPS if text.startswith(op, i)), None)
            if matched_inner is not None:
                i += len(matched_inner)
                continue

            if text.startswith(">>", i):
                if depth == 1:
                    i += 2
                    continue
                if (
                    i > 0
                    and text[i - 1].isspace()
                    and i + 2 < n
                    and text[i + 2].isspace()
                    and _has_unmatched_closer_ahead(text, i + 2, depth - 2)
                ):
                    i += 2
                    continue

            if ch == "<":
                if not _is_template_less_than_comparison(text, i):
                    depth += 1
            elif ch == ">":
                k = i + 1
                while k < n and text[k].isspace():
                    k += 1
                next_char = text[k] if k < n else ""
                is_digit_rhs = next_char.isdigit()
                is_spaced_cmp = (
                    i > 0
                    and text[i - 1].isspace()
                    and i + 1 < n
                    and text[i + 1].isspace()
                    and next_char not in {",", ">", ":", "(", ")", "[", "]", "&", "*", ";", ""}
                    and _has_unmatched_closer_ahead(text, i + 1, depth - 1)
                )
                if not is_digit_rhs and not is_spaced_cmp:
                    depth -= 1
            i += 1

    return "".join(out)


def normalize_function_name(func: str | None) -> str:
    """Normalize a C++ symbol/function name into an ASLR- and template-stable representation."""
    if not func:
        return ""
    s = str(func).strip()
    if not s:
        return ""

    s = _OFFSET_TAIL_RE.sub("", s).strip()
    s = _LAMBDA_HASH_RE.sub("__LAMBDA__", s)
    s = _HEX_ADDR_RE.sub("<addr>", s)
    s = _collapse_templates(s)
    s = s.replace("__LAMBDA__", "<lambda>")
    s = _MULTISPACE_RE.sub(" ", s).strip()
    return s


def resolve_noise_rules(
    project_settings: dict[str, Any] | None = None,
    system_settings: dict[str, Any] | None = None,
) -> NoiseRules:
    """Layer system default noise rules with per-project overrides/extensions."""
    sys_cfg = system_settings if isinstance(system_settings, dict) else {}
    proj_cfg = project_settings if isinstance(project_settings, dict) else {}

    replace_defaults = bool(proj_cfg.get("replace_default_noise", False))

    if replace_defaults:
        base_modules: list[str] = []
        base_functions: list[str] = []
    else:
        raw_sys_mods = sys_cfg.get("default_noise_modules")
        raw_sys_funcs = sys_cfg.get("default_noise_functions")
        base_modules = (
            [str(m) for m in raw_sys_mods if m and str(m).strip()]
            if raw_sys_mods is not None
            else list(DEFAULT_NOISE_MODULES)
        )
        base_functions = (
            [str(f) for f in raw_sys_funcs if f and str(f).strip()]
            if raw_sys_funcs is not None
            else list(DEFAULT_NOISE_FUNCTIONS)
        )

    modules: set[str] = set()
    for mod in base_modules:
        norm = _cross_platform_basename(mod)
        if norm:
            modules.add(norm)

    functions: list[str] = []
    seen_funcs: set[str] = set()
    for fn in base_functions:
        cleaned = str(fn).strip()
        if cleaned and cleaned not in seen_funcs:
            seen_funcs.add(cleaned)
            functions.append(cleaned)

    for mod in proj_cfg.get("noise_modules") or []:
        norm = _cross_platform_basename(str(mod))
        if norm:
            modules.add(norm)

    for fn in proj_cfg.get("noise_functions") or []:
        cleaned = str(fn).strip()
        if cleaned and cleaned not in seen_funcs:
            seen_funcs.add(cleaned)
            functions.append(cleaned)

    for handler in proj_cfg.get("custom_crash_handlers") or []:
        cleaned = str(handler).strip()
        if not cleaned:
            continue
        if cleaned.lower().endswith((".dll", ".exe", ".so", ".dylib")):
            norm = _cross_platform_basename(cleaned)
            if norm:
                modules.add(norm)
        if cleaned not in seen_funcs:
            seen_funcs.add(cleaned)
            functions.append(cleaned)

    return NoiseRules(
        noise_modules=frozenset(modules),
        noise_functions=tuple(functions),
    )


def _coerce_noise_rules(noise_rules: NoiseRules | dict[str, Any] | None) -> NoiseRules:
    if isinstance(noise_rules, NoiseRules):
        return noise_rules
    if isinstance(noise_rules, dict):
        if "noise_modules" in noise_rules or "noise_functions" in noise_rules:
            return resolve_noise_rules(
                project_settings={
                    "replace_default_noise": bool(noise_rules.get("replace_default_noise", False)),
                    "noise_modules": noise_rules.get("noise_modules") or [],
                    "noise_functions": noise_rules.get("noise_functions") or [],
                    "custom_crash_handlers": noise_rules.get("custom_crash_handlers") or [],
                },
                system_settings=None,
            )
        return resolve_noise_rules(project_settings=None, system_settings=noise_rules)
    return resolve_noise_rules()


def _is_noise_module(mod: str, rules: NoiseRules) -> bool:
    if not mod:
        return False
    mod_lower = mod.lower()
    for pat in rules.noise_modules:
        if "*" in pat or "?" in pat:
            if fnmatch.fnmatchcase(mod_lower, pat):
                return True
        elif mod_lower == pat:
            return True
    return False


def _matches_function_pattern(candidate: str, pattern: str) -> bool:
    if not candidate or not pattern:
        return False
    bare = candidate.split("(", 1)[0].strip()
    if "*" in pattern or "?" in pattern:
        if fnmatch.fnmatchcase(candidate, pattern) or fnmatch.fnmatchcase(bare, pattern):
            return True
        cand_l = candidate.lower()
        bare_l = bare.lower()
        pat_l = pattern.lower()
        return fnmatch.fnmatchcase(cand_l, pat_l) or fnmatch.fnmatchcase(bare_l, pat_l)

    if candidate == pattern or bare == pattern:
        return True
    if bare.endswith(f"::{pattern}") or candidate.startswith(f"{pattern}("):
        return True
    pat_l = pattern.lower()
    bare_l = bare.lower()
    return bare_l == pat_l or bare_l.endswith(f"::{pat_l}")


def _is_noise_function(
    raw_func: str | None,
    norm_func: str,
    rules: NoiseRules,
) -> bool:
    raw_clean = _OFFSET_TAIL_RE.sub("", str(raw_func).strip()).strip() if raw_func else ""
    for pat in rules.noise_functions:
        if norm_func and _matches_function_pattern(norm_func, pat):
            return True
        if raw_clean and _matches_function_pattern(raw_clean, pat):
            return True
    return False


def filter_and_normalize_frames(
    parsed_stacks_json: dict[str, Any] | None,
    noise_rules: NoiseRules | dict[str, Any] | None = None,
    crash_reason: str | None = None,
) -> list[dict[str, Any]]:
    """Expand inline frames, normalize function names/offsets, and filter noise frames."""
    rules = _coerce_noise_rules(noise_rules)

    threads = (parsed_stacks_json or {}).get("threads") or []
    raw_frames: list[dict[str, Any]] = []
    if isinstance(threads, list) and threads:
        raw_idx = (parsed_stacks_json or {}).get("crashing_thread_index")
        try:
            idx = int(raw_idx) if raw_idx is not None else 0
        except (TypeError, ValueError):
            idx = 0
        if idx < 0 or idx >= len(threads):
            idx = 0
        chosen_thread = threads[idx]
        if isinstance(chosen_thread, dict):
            raw_frames = list(chosen_thread.get("frames") or [])

    candidates: list[tuple[dict[str, Any], bool, bool]] = []

    def _build_entry(
        raw_mod: str | None,
        raw_func: str | None,
        raw_mod_offset: str | None,
        is_inline: bool,
        missing_symbols: bool,
        raw_file: str | None,
        raw_line: int | None,
    ) -> None:
        mod = _cross_platform_basename(raw_mod) or "unknown"
        norm_fn = normalize_function_name(raw_func)
        if not norm_fn:
            if raw_mod_offset is not None and str(raw_mod_offset).strip():
                norm_fn = str(raw_mod_offset).strip().lower()
            else:
                norm_fn = "unknown"
        normalized = f"{mod}!{norm_fn}"
        entry = {
            "module": mod,
            "function": norm_fn,
            "normalized": normalized,
            "is_inline": is_inline,
            "missing_symbols": bool(missing_symbols),
            "file": raw_file,
            "line": raw_line,
        }
        mod_is_noise = _is_noise_module(mod, rules)
        fn_is_noise = _is_noise_function(raw_func, norm_fn, rules)
        candidates.append((entry, mod_is_noise, fn_is_noise))

    for frame in raw_frames:
        if not isinstance(frame, dict):
            continue
        parent_mod = frame.get("module")
        parent_mod_offset = frame.get("module_offset")

        for inline in frame.get("inlines") or []:
            if not isinstance(inline, dict):
                continue
            _build_entry(
                raw_mod=inline.get("module") or parent_mod,
                raw_func=inline.get("function"),
                raw_mod_offset=inline.get("module_offset") or parent_mod_offset,
                is_inline=True,
                missing_symbols=bool(inline.get("missing_symbols", False)),
                raw_file=inline.get("file"),
                raw_line=inline.get("line"),
            )

        _build_entry(
            raw_mod=parent_mod,
            raw_func=frame.get("function"),
            raw_mod_offset=parent_mod_offset,
            is_inline=False,
            missing_symbols=bool(frame.get("missing_symbols", False)),
            raw_file=frame.get("file"),
            raw_line=frame.get("line"),
        )

    # Primary: keep frames matching neither noise module nor noise function.
    primary = [entry for entry, mod_noise, fn_noise in candidates if not mod_noise and not fn_noise]
    if primary:
        return primary

    # Fallback 1: keep non-noise-module frames even if function matched noise filter.
    non_noise_mod = [entry for entry, mod_noise, _ in candidates if not mod_noise]
    if non_noise_mod:
        return non_noise_mod

    # Fallback 2: keep top 5 unfiltered frames (e.g. pure OS DLL crash).
    if candidates:
        return [entry for entry, _, _ in candidates[:5]]

    # Fallback 3: empty stack -> deterministic synthetic frame from crash_reason.
    exc_cat = normalize_exception_category(crash_reason)
    return [
        {
            "module": "unknown",
            "function": exc_cat,
            "normalized": f"unknown!{exc_cat}",
            "is_inline": False,
            "missing_symbols": True,
            "file": None,
            "line": None,
        }
    ]


def compute_signature_hash(
    project_id: uuid.UUID | str,
    crash_reason: str | None,
    normalized_frames: list[dict[str, Any]],
) -> str:
    """Compute a deterministic SHA-256 cluster signature hash scoped to project_id."""
    exc_cat = normalize_exception_category(crash_reason)
    top_5: list[str] = []
    for frame in normalized_frames[:5]:
        if isinstance(frame, dict):
            top_5.append(
                str(
                    frame.get("normalized")
                    or f"{frame.get('module', 'unknown')}!{frame.get('function', 'unknown')}"
                )
            )
        else:
            top_5.append(str(frame))
    payload = f"{project_id}:{exc_cat}:{'|'.join(top_5)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _trigram_set(text: str) -> set[str]:
    if not text:
        return set()
    grams: set[str] = set()
    for token in _TRGM_TOKEN_RE.findall(text.lower()):
        padded = f"  {token} "
        for idx in range(len(padded) - 2):
            grams.add(padded[idx : idx + 3])
    return grams


def _trigram_similarity(a: str, b: str) -> float:
    """Pure-Python approximation of PostgreSQL pg_trgm word-padded trigram similarity."""
    ta = _trigram_set(a)
    tb = _trigram_set(b)
    if not ta or not tb:
        return 0.0
    union = ta | tb
    if not union:
        return 0.0
    return len(ta & tb) / len(union)


def _frame_key(frame: Any) -> str:
    if isinstance(frame, dict):
        if frame.get("normalized"):
            return str(frame["normalized"])
        return f"{frame.get('module', 'unknown')}!{frame.get('function', 'unknown')}"
    return str(frame)


def _weighted_top10_jaccard(
    frames_a: list[Any],
    frames_b: list[Any],
) -> float:
    """Position-weighted Jaccard over top 10 normalized frames (top frames weigh more)."""
    if not frames_a or not frames_b:
        return 0.0

    weights_a: dict[str, float] = {}
    for idx, frame in enumerate(frames_a[:10]):
        key = _frame_key(frame)
        w = 1.0 / (1.0 + 0.25 * idx)
        if w > weights_a.get(key, 0.0):
            weights_a[key] = w

    weights_b: dict[str, float] = {}
    for idx, frame in enumerate(frames_b[:10]):
        key = _frame_key(frame)
        w = 1.0 / (1.0 + 0.25 * idx)
        if w > weights_b.get(key, 0.0):
            weights_b[key] = w

    all_keys = weights_a.keys() | weights_b.keys()
    if not all_keys:
        return 0.0
    numerator = sum(min(weights_a.get(k, 0.0), weights_b.get(k, 0.0)) for k in all_keys)
    denominator = sum(max(weights_a.get(k, 0.0), weights_b.get(k, 0.0)) for k in all_keys)
    if denominator <= 0.0:
        return 0.0
    return numerator / denominator


async def recall_similar_issues(
    session: AsyncSession,
    project_id: uuid.UUID,
    normalized_frames: list[dict[str, Any]],
    query_text: str = "",
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Recall top-k similar known issues using weighted Top-10 Jaccard + pg_trgm/trigram."""
    if limit <= 0:
        return []

    top10_frames = normalized_frames[:10]
    stack_query_text = " ".join(_frame_key(f) for f in top10_frames).strip()
    clean_query = (query_text or "").strip()
    trgm_query = f"{clean_query} {stack_query_text}".strip()

    bind = session.get_bind()
    is_pg = bind is not None and bind.dialect.name == "postgresql"

    issues_by_id: dict[int, Issue] = {}
    clusters_by_issue: dict[int, list[CrashCluster]] = {}
    cluster_trgm_scores: dict[uuid.UUID, float] = {}
    issue_pg_trgm_scores: dict[int, float] = {}

    if is_pg:
        if stack_query_text:
            sim_col = func.similarity(CrashCluster.signature_text, stack_query_text).label("sim")
            cluster_stmt = (
                select(CrashCluster, Issue, sim_col)
                .join(Issue, CrashCluster.issue_id == Issue.id)
                .where(
                    CrashCluster.project_id == project_id,
                    CrashCluster.issue_id.is_not(None),
                )
                .order_by(sim_col.desc(), Issue.crash_count.desc())
                .limit(max(limit * 6, 30))
            )
            for cluster, issue, sim in (await session.execute(cluster_stmt)).all():
                issues_by_id[issue.id] = issue
                clusters_by_issue.setdefault(issue.id, []).append(cluster)
                cluster_trgm_scores[cluster.id] = float(sim or 0.0)
        else:
            cluster_stmt = (
                select(CrashCluster, Issue)
                .join(Issue, CrashCluster.issue_id == Issue.id)
                .where(
                    CrashCluster.project_id == project_id,
                    CrashCluster.issue_id.is_not(None),
                )
                .order_by(Issue.crash_count.desc(), Issue.last_seen_at.desc())
                .limit(max(limit * 6, 30))
            )
            for cluster, issue in (await session.execute(cluster_stmt)).all():
                issues_by_id[issue.id] = issue
                clusters_by_issue.setdefault(issue.id, []).append(cluster)

        if trgm_query:
            issue_sim = func.greatest(
                func.similarity(Issue.title, trgm_query),
                func.similarity(func.coalesce(Issue.root_cause, ""), trgm_query),
            ).label("issue_sim")
            issue_stmt = (
                select(Issue, issue_sim)
                .where(Issue.project_id == project_id)
                .order_by(issue_sim.desc(), Issue.crash_count.desc())
                .limit(max(limit * 4, 20))
            )
            for issue, sim in (await session.execute(issue_stmt)).all():
                issues_by_id[issue.id] = issue
                issue_pg_trgm_scores[issue.id] = float(sim or 0.0)
        else:
            issue_stmt = (
                select(Issue)
                .where(Issue.project_id == project_id)
                .order_by(Issue.crash_count.desc(), Issue.last_seen_at.desc())
                .limit(max(limit * 4, 20))
            )
            for issue in (await session.execute(issue_stmt)).scalars().all():
                issues_by_id[issue.id] = issue
    else:
        issue_rows = (
            await session.execute(select(Issue).where(Issue.project_id == project_id))
        ).scalars().all()
        for issue in issue_rows:
            issues_by_id[issue.id] = issue

        cluster_rows = (
            await session.execute(
                select(CrashCluster).where(
                    CrashCluster.project_id == project_id,
                    CrashCluster.issue_id.is_not(None),
                )
            )
        ).scalars().all()
        for cluster in cluster_rows:
            if cluster.issue_id is not None and cluster.issue_id in issues_by_id:
                clusters_by_issue.setdefault(cluster.issue_id, []).append(cluster)
                cluster_trgm_scores[cluster.id] = (
                    _trigram_similarity(stack_query_text, cluster.signature_text or "")
                    if stack_query_text
                    else 0.0
                )

    if not issues_by_id:
        return []

    scored: list[tuple[float, int, datetime, dict[str, Any]]] = []
    q_lower = clean_query.lower()

    for issue_id, issue in issues_by_id.items():
        linked_clusters = clusters_by_issue.get(issue_id, [])

        best_jaccard = 0.0
        best_cluster_trgm = 0.0
        best_cluster_id: str | None = None
        best_cluster_signal = -1.0

        for cluster in linked_clusters:
            jacc = _weighted_top10_jaccard(
                top10_frames,
                cluster.signature_frames_json or [],
            )
            ctrgm = cluster_trgm_scores.get(cluster.id, 0.0)
            if jacc > best_jaccard:
                best_jaccard = jacc
            if ctrgm > best_cluster_trgm:
                best_cluster_trgm = ctrgm
            signal = 0.7 * jacc + 0.3 * ctrgm
            if signal > best_cluster_signal:
                best_cluster_signal = signal
                best_cluster_id = str(cluster.id)

        if is_pg and issue_id in issue_pg_trgm_scores:
            issue_text_trgm = issue_pg_trgm_scores[issue_id]
        else:
            issue_doc = " ".join(
                [
                    issue.title or "",
                    issue.root_cause or "",
                    issue.component or "",
                    " ".join(issue.log_patterns_json or []),
                ]
            ).strip()
            issue_text_trgm = _trigram_similarity(trgm_query, issue_doc) if trgm_query else 0.0

        trigram_score = max(best_cluster_trgm, issue_text_trgm)
        substring_boost = 0.0
        if q_lower:
            haystack = (
                f"{issue.title or ''} {issue.root_cause or ''} {issue.component or ''}"
            ).lower()
            if q_lower in haystack:
                substring_boost = 0.5

        if top10_frames:
            combined_score = (
                0.60 * best_jaccard
                + 0.25 * best_cluster_trgm
                + 0.15 * issue_text_trgm
                + (0.15 * substring_boost)
            )
        elif q_lower:
            combined_score = issue_text_trgm + substring_boost
        else:
            combined_score = 0.0

        entry = {
            "issue_id": issue.id,
            "title": issue.title,
            "status": issue.status,
            "component": issue.component,
            "root_cause": issue.root_cause,
            "fixed_in_version": issue.fixed_in_version,
            "is_ambiguous_stack": issue.is_ambiguous_stack,
            "crash_count": issue.crash_count,
            "log_patterns": list(issue.log_patterns_json or []),
            "score": round(float(combined_score), 6),
            "jaccard_score": round(float(best_jaccard), 6),
            "trigram_score": round(float(trigram_score), 6),
            "matched_cluster_id": best_cluster_id,
        }
        scored.append(
            (
                float(combined_score),
                int(issue.crash_count or 0),
                _as_utc(issue.last_seen_at),
                entry,
            )
        )

    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return [item[3] for item in scored[:limit]]


async def cluster_crash(
    session: AsyncSession,
    crash_report: CrashReport,
    project: Project,
    triage_mode: str | None = None,
    system_settings: dict[str, Any] | None = None,
    noise_rules: NoiseRules | dict[str, Any] | None = None,
) -> ClusterResult:
    """Cluster a parsed CrashReport, reconcile counts, and decide AI triage eligibility."""
    rules = (
        _coerce_noise_rules(noise_rules)
        if noise_rules is not None
        else resolve_noise_rules(project.settings_json, system_settings)
    )
    normalized_frames = filter_and_normalize_frames(
        crash_report.parsed_stacks_json,
        rules,
        crash_reason=crash_report.crash_reason,
    )
    top_10 = normalized_frames[:10]
    sig_hash = compute_signature_hash(project.id, crash_report.crash_reason, top_10)
    sig_text = " ".join(_frame_key(f) for f in top_10) or normalize_exception_category(
        crash_report.crash_reason
    )
    exc_cat = normalize_exception_category(crash_report.crash_reason)

    await session.flush()
    old_cluster_id = crash_report.cluster_id
    old_issue_id = crash_report.issue_id

    existing_cluster = (
        await session.execute(
            select(CrashCluster).where(CrashCluster.signature_hash == sig_hash)
        )
    ).scalar_one_or_none()

    is_new_cluster = False
    if existing_cluster is None:
        try:
            async with session.begin_nested():
                cluster = CrashCluster(
                    project_id=project.id,
                    issue_id=None,
                    signature_hash=sig_hash,
                    signature_frames_json=top_10,
                    signature_text=sig_text,
                    exception_type=exc_cat,
                    crash_count=0,
                )
                session.add(cluster)
                await session.flush()
            is_new_cluster = True
        except IntegrityError:
            cluster = (
                await session.execute(
                    select(CrashCluster).where(CrashCluster.signature_hash == sig_hash)
                )
            ).scalar_one()
    else:
        cluster = existing_cluster
        if not cluster.signature_frames_json:
            cluster.signature_frames_json = top_10
        if not cluster.signature_text:
            cluster.signature_text = sig_text
        if not cluster.exception_type:
            cluster.exception_type = exc_cat

    # Reconcile cluster count atomically and idempotently.
    if old_cluster_id != cluster.id:
        cond_old_cluster = (
            CrashReport.cluster_id.is_(None)
            if old_cluster_id is None
            else CrashReport.cluster_id == old_cluster_id
        )
        cluster_upd = await session.execute(
            update(CrashReport)
            .where(CrashReport.id == crash_report.id, cond_old_cluster)
            .values(cluster_id=cluster.id)
        )
        if (cluster_upd.rowcount or 0) > 0:
            if old_cluster_id is not None:
                await session.execute(
                    update(CrashCluster)
                    .where(CrashCluster.id == old_cluster_id)
                    .values(
                        crash_count=case(
                            (CrashCluster.crash_count > 0, CrashCluster.crash_count - 1),
                            else_=0,
                        )
                    )
                )
                old_cluster = await session.get(CrashCluster, old_cluster_id)
                if old_cluster is not None:
                    await session.refresh(old_cluster, attribute_names=["crash_count"])
            await session.execute(
                update(CrashCluster)
                .where(CrashCluster.id == cluster.id)
                .values(crash_count=CrashCluster.crash_count + 1)
            )
            await session.refresh(cluster, attribute_names=["crash_count"])
        await session.refresh(crash_report, attribute_names=["cluster_id"])
        if old_cluster_id is not None:
            crash_report.ai_verdict = None
            crash_report.ai_confidence = None

    # Reconcile issue link & counts atomically, keeping CrashReport.issue_id synced for GC.
    linked_issue = (
        await session.get(Issue, cluster.issue_id) if cluster.issue_id is not None else None
    )
    if linked_issue is not None:
        if old_issue_id != linked_issue.id:
            cond_old_issue = (
                CrashReport.issue_id.is_(None)
                if old_issue_id is None
                else CrashReport.issue_id == old_issue_id
            )
            issue_upd = await session.execute(
                update(CrashReport)
                .where(CrashReport.id == crash_report.id, cond_old_issue)
                .values(issue_id=linked_issue.id)
            )
            if (issue_upd.rowcount or 0) > 0:
                if old_issue_id is not None:
                    await session.execute(
                        update(Issue)
                        .where(Issue.id == old_issue_id)
                        .values(
                            crash_count=case(
                                (Issue.crash_count > 0, Issue.crash_count - 1),
                                else_=0,
                            )
                        )
                    )
                    old_issue = await session.get(Issue, old_issue_id)
                    if old_issue is not None:
                        await session.refresh(old_issue, attribute_names=["crash_count"])
                await session.execute(
                    update(Issue)
                    .where(Issue.id == linked_issue.id)
                    .values(crash_count=Issue.crash_count + 1)
                )
                await session.refresh(linked_issue, attribute_names=["crash_count"])
            await session.refresh(crash_report, attribute_names=["issue_id"])
        crash_ts = _as_utc(crash_report.crash_time)
        if _as_utc(linked_issue.last_seen_at) < crash_ts:
            linked_issue.last_seen_at = crash_ts
    else:
        if old_issue_id is not None and old_cluster_id != cluster.id:
            issue_upd = await session.execute(
                update(CrashReport)
                .where(CrashReport.id == crash_report.id, CrashReport.issue_id == old_issue_id)
                .values(issue_id=None)
            )
            if (issue_upd.rowcount or 0) > 0:
                await session.execute(
                    update(Issue)
                    .where(Issue.id == old_issue_id)
                    .values(
                        crash_count=case(
                            (Issue.crash_count > 0, Issue.crash_count - 1),
                            else_=0,
                        )
                    )
                )
                old_issue = await session.get(Issue, old_issue_id)
                if old_issue is not None:
                    await session.refresh(old_issue)
            await session.refresh(crash_report, attribute_names=["issue_id"])

    raw_mode = (
        triage_mode
        or (project.settings_json or {}).get("ai_triage_mode")
        or (system_settings or {}).get("ai_triage_mode")
        or "SMART_TRIAGE"
    )
    effective_mode = str(raw_mode).strip().upper()
    if effective_mode not in {"SMART_TRIAGE", "ALWAYS", "MANUAL"}:
        raise ClusteringError(f"Unsupported ai_triage_mode: {raw_mode!r}")

    if effective_mode == "MANUAL":
        should_run_ai = False
        trigger_reason = "manual_mode"
        crash_report.status = "ANALYZED"
    elif effective_mode == "ALWAYS":
        should_run_ai = True
        trigger_reason = "always_mode"
    else:
        # SMART_TRIAGE
        if linked_issue is None:
            should_run_ai = True
            trigger_reason = "new_or_unassigned_cluster"
        elif linked_issue.is_ambiguous_stack:
            should_run_ai = True
            trigger_reason = "ambiguous_stack"
        elif linked_issue.status == "FIXED" and _is_post_fix_version(
            crash_report.app_version,
            linked_issue.fixed_in_version,
        ):
            should_run_ai = True
            trigger_reason = "potential_regression"
        else:
            should_run_ai = False
            trigger_reason = "known_unambiguous_issue"
            crash_report.status = "ANALYZED"
            crash_report.ai_verdict = "KNOWN_ISSUE"

    similar_issues = await recall_similar_issues(
        session,
        project.id,
        top_10,
        limit=5,
    )
    await session.flush()

    return ClusterResult(
        cluster=cluster,
        issue=linked_issue,
        signature_hash=sig_hash,
        signature_text=sig_text,
        normalized_frames=normalized_frames,
        is_new_cluster=is_new_cluster,
        should_run_ai=should_run_ai,
        trigger_reason=trigger_reason,
        similar_issues=similar_issues,
    )


async def apply_human_verdict(
    session: AsyncSession,
    crash_report: CrashReport,
    action: str,
    issue_id: int | None = None,
    new_issue_data: dict[str, Any] | None = None,
    root_cause: str | None = None,
    add_log_patterns: list[str] | None = None,
    is_ambiguous_stack: bool | None = None,
    commit: bool = True,
) -> CrashReport:
    """Apply a human engineer verdict and feed knowledge back into Issues and Clusters."""
    if action not in {"confirm", "assign_issue", "create_issue"}:
        raise ClusteringError(f"Invalid verdict action: {action!r}")

    cluster = (
        await session.get(CrashCluster, crash_report.cluster_id)
        if crash_report.cluster_id is not None
        else None
    )
    old_issue_id = crash_report.issue_id
    old_issue = await session.get(Issue, old_issue_id) if old_issue_id is not None else None
    crash_ts = _as_utc(crash_report.crash_time)

    target_issue: Issue | None = None

    if action == "confirm":
        target_issue = old_issue
        if target_issue is None and cluster is not None and cluster.issue_id is not None:
            target_issue = await session.get(Issue, cluster.issue_id)
        if target_issue is None:
            raise ClusteringError("Cannot confirm verdict: crash report has no linked issue")
        if target_issue.project_id != crash_report.project_id:
            raise ClusteringError("Target issue belongs to a different project")
        if old_issue_id != target_issue.id:
            if old_issue is not None:
                old_issue.crash_count = max(0, (old_issue.crash_count or 0) - 1)
            crash_report.issue_id = target_issue.id
            target_issue.crash_count = (target_issue.crash_count or 0) + 1
        if target_issue.status == "NEW":
            target_issue.status = "KNOWN"
        if _as_utc(target_issue.last_seen_at) < crash_ts:
            target_issue.last_seen_at = crash_ts

    elif action == "assign_issue":
        if issue_id is None:
            raise ClusteringError("issue_id is required when action='assign_issue'")
        target_issue = await session.get(Issue, issue_id)
        if target_issue is None or target_issue.project_id != crash_report.project_id:
            raise ClusteringError("Target issue not found in crash report project")
        if old_issue_id != target_issue.id:
            if old_issue is not None:
                old_issue.crash_count = max(0, (old_issue.crash_count or 0) - 1)
            target_issue.crash_count = (target_issue.crash_count or 0) + 1
            crash_report.issue_id = target_issue.id
        if _as_utc(target_issue.last_seen_at) < crash_ts:
            target_issue.last_seen_at = crash_ts

    elif action == "create_issue":
        if not new_issue_data or not str(new_issue_data.get("title") or "").strip():
            raise ClusteringError("new_issue_data with a non-empty title is required")
        title = str(new_issue_data["title"]).strip()[:512]
        raw_status = new_issue_data.get("status")
        status_val = "KNOWN" if raw_status is None else str(raw_status).strip().upper()
        if status_val not in VALID_ISSUE_STATUSES:
            raise ClusteringError(f"Invalid issue status: {raw_status!r}")
        raw_comp = new_issue_data.get("component")
        comp_val = (
            str(raw_comp).strip()[:128]
            if raw_comp is not None and str(raw_comp).strip()
            else None
        )
        target_issue = Issue(
            project_id=crash_report.project_id,
            title=title,
            status=status_val,
            component=comp_val,
            root_cause=new_issue_data.get("root_cause"),
            log_patterns_json=[],
            is_ambiguous_stack=bool(is_ambiguous_stack or False),
            is_pinned=False,
            crash_count=1,
            first_seen_at=crash_ts,
            last_seen_at=crash_ts,
        )
        session.add(target_issue)
        await session.flush()
        crash_report.issue_id = target_issue.id

    assert target_issue is not None

    crash_report.is_human_verified = True
    if crash_report.status != "ANALYZED":
        crash_report.status = "ANALYZED"

    if root_cause is not None:
        target_issue.root_cause = root_cause

    if add_log_patterns:
        existing_patterns = list(target_issue.log_patterns_json or [])
        cleaned_new = [str(p).strip() for p in add_log_patterns if p and str(p).strip()]
        target_issue.log_patterns_json = list(dict.fromkeys([*existing_patterns, *cleaned_new]))

    if is_ambiguous_stack is not None:
        target_issue.is_ambiguous_stack = bool(is_ambiguous_stack)

    effective_ambiguous = (
        bool(is_ambiguous_stack)
        if is_ambiguous_stack is not None
        else bool(target_issue.is_ambiguous_stack)
    )

    await session.flush()
    for affected_issue_id in {old_issue_id, target_issue.id}:
        if affected_issue_id is not None:
            exact_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(CrashReport)
                        .where(CrashReport.issue_id == affected_issue_id)
                    )
                ).scalar_one()
            )
            affected_issue = await session.get(Issue, affected_issue_id)
            if affected_issue is not None:
                affected_issue.crash_count = exact_count

    if cluster is not None:
        if effective_ambiguous:
            cluster.issue_id = None
        elif action == "create_issue":
            other_issue_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(CrashReport)
                        .where(
                            CrashReport.cluster_id == cluster.id,
                            CrashReport.id != crash_report.id,
                            CrashReport.issue_id.is_not(None),
                            CrashReport.issue_id != target_issue.id,
                        )
                    )
                ).scalar_one()
            )
            if other_issue_count > 0:
                cluster.issue_id = None
            else:
                cluster.issue_id = target_issue.id
        else:
            cluster.issue_id = target_issue.id

    await session.flush()
    if commit:
        await session.commit()
        await session.refresh(crash_report)
    return crash_report


class ClusteringService:
    """Class facade exposing clustering, fuzzy recall, and human verdict operations."""

    resolve_noise_rules = staticmethod(resolve_noise_rules)
    normalize_function_name = staticmethod(normalize_function_name)
    filter_and_normalize_frames = staticmethod(filter_and_normalize_frames)
    compute_signature_hash = staticmethod(compute_signature_hash)
    recall_similar_issues = staticmethod(recall_similar_issues)
    cluster_crash = staticmethod(cluster_crash)
    apply_human_verdict = staticmethod(apply_human_verdict)
