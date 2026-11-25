"""Pure C++ stack frame normalization, noise filtering, and signature hashing."""

from __future__ import annotations

import fnmatch
import hashlib
import re
import uuid
from typing import Any

from app.services.clustering.types import NoiseRules, _cross_platform_basename
from app.services.settings_service import (
    DEFAULT_NOISE_FUNCTIONS,
    DEFAULT_NOISE_MODULES,
)

_OFFSET_TAIL_RE = re.compile(r"(?:\s*[+@]\s*(?:0x[0-9a-fA-F]+|\d+))+$")
_LAMBDA_HASH_RE = re.compile(r"<lambda_[0-9a-fA-F]+>|lambda_[0-9a-fA-F]{6,}")
_HEX_ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]{4,}\b")
_MULTISPACE_RE = re.compile(r"\s+")
_EXC_ADDR_SUFFIX_RE = re.compile(r"\s*(?:/|@)\s*0x[0-9a-fA-F]+\s*$")

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


def _frame_key(frame: Any) -> str:
    if isinstance(frame, dict):
        if frame.get("normalized"):
            return str(frame["normalized"])
        return f"{frame.get('module', 'unknown')}!{frame.get('function', 'unknown')}"
    return str(frame)


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
