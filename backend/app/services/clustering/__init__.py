"""Deterministic crash stack filtering, clustering, fuzzy recall, and human KB feedback."""

from __future__ import annotations

from app.services.clustering.engine import apply_human_verdict, cluster_crash
from app.services.clustering.normalization import (
    _coerce_noise_rules,
    _collapse_templates,
    _frame_key,
    _is_noise_function,
    _is_noise_module,
    _matches_function_pattern,
    compute_signature_hash,
    filter_and_normalize_frames,
    normalize_exception_category,
    normalize_function_name,
    resolve_noise_rules,
)
from app.services.clustering.recall import (
    _trigram_set,
    _trigram_similarity,
    _weighted_top10_jaccard,
    recall_similar_issues,
)
from app.services.clustering.types import (
    VALID_ISSUE_STATUSES,
    ClusteringError,
    ClusterResult,
    NoiseRules,
    _as_utc,
    _cross_platform_basename,
)
from app.services.clustering.versions import (
    _compare_versions,
    _is_post_fix_version,
    _parse_version,
)
from app.services.settings_service import (
    DEFAULT_NOISE_FUNCTIONS,
    DEFAULT_NOISE_MODULES,
)


class ClusteringService:
    """Class facade exposing clustering, fuzzy recall, and human verdict operations."""

    resolve_noise_rules = staticmethod(resolve_noise_rules)
    normalize_function_name = staticmethod(normalize_function_name)
    filter_and_normalize_frames = staticmethod(filter_and_normalize_frames)
    compute_signature_hash = staticmethod(compute_signature_hash)
    recall_similar_issues = staticmethod(recall_similar_issues)
    cluster_crash = staticmethod(cluster_crash)
    apply_human_verdict = staticmethod(apply_human_verdict)


__all__ = [
    "ClusteringError",
    "ClusteringService",
    "ClusterResult",
    "DEFAULT_NOISE_FUNCTIONS",
    "DEFAULT_NOISE_MODULES",
    "NoiseRules",
    "VALID_ISSUE_STATUSES",
    "_as_utc",
    "_coerce_noise_rules",
    "_collapse_templates",
    "_compare_versions",
    "_cross_platform_basename",
    "_frame_key",
    "_is_noise_function",
    "_is_noise_module",
    "_is_post_fix_version",
    "_matches_function_pattern",
    "_parse_version",
    "_trigram_set",
    "_trigram_similarity",
    "_weighted_top10_jaccard",
    "apply_human_verdict",
    "cluster_crash",
    "compute_signature_hash",
    "filter_and_normalize_frames",
    "normalize_exception_category",
    "normalize_function_name",
    "recall_similar_issues",
    "resolve_noise_rules",
]
