"""Core types, constants, and path/time helpers for crash clustering."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.db.base import utcnow
from app.db.models import CrashCluster, Issue

VALID_ISSUE_STATUSES: frozenset[str] = frozenset(
    {"NEW", "KNOWN", "FIXED", "WONTFIX", "REGRESSION"}
)


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
