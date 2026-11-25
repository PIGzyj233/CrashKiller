"""Issue status workflow helpers and transition history recording."""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import Issue, IssueStatusEvent

_UNSET: Final[object] = object()

VALID_EVENT_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "manual",
        "batch",
        "form_edit",
        "ai_triage",
        "ai_regression",
        "human_verdict",
        "split",
    }
)


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def record_issue_status_event(
    session: AsyncSession,
    *,
    issue_id: int,
    from_status: str | None,
    to_status: str,
    source: str = "manual",
    fixed_in_version: str | None = None,
    note: str | None = None,
    crash_id: uuid.UUID | None = None,
) -> IssueStatusEvent:
    """Append an IssueStatusEvent row to the current DB session."""
    normalized_source = source if source in VALID_EVENT_SOURCES else "manual"
    event = IssueStatusEvent(
        issue_id=issue_id,
        from_status=from_status,
        to_status=to_status,
        source=normalized_source,
        fixed_in_version=_clean_optional_text(fixed_in_version),
        note=_clean_optional_text(note),
        crash_id=crash_id,
        created_at=utcnow(),
    )
    session.add(event)
    return event


def transition_issue_status(
    session: AsyncSession,
    issue: Issue,
    *,
    to_status: str,
    source: str = "manual",
    fixed_in_version: str | None | object = _UNSET,
    clear_fixed_in_version: bool = False,
    note: str | None = None,
    crash_id: uuid.UUID | None = None,
) -> IssueStatusEvent:
    """Transition an issue's status, optionally update fixed_in_version, and record history."""
    previous_status = issue.status
    issue.status = to_status

    if clear_fixed_in_version:
        issue.fixed_in_version = None
    elif fixed_in_version is not _UNSET:
        assert fixed_in_version is None or isinstance(fixed_in_version, str)
        cleaned_version = _clean_optional_text(fixed_in_version)
        if cleaned_version is not None:
            issue.fixed_in_version = cleaned_version

    recorded_version = (
        issue.fixed_in_version if to_status in {"FIXED", "REGRESSION"} else None
    )
    if fixed_in_version is not _UNSET and isinstance(fixed_in_version, str):
        explicit_ver = _clean_optional_text(fixed_in_version)
        if explicit_ver is not None:
            recorded_version = explicit_ver

    return record_issue_status_event(
        session,
        issue_id=issue.id,
        from_status=previous_status,
        to_status=to_status,
        source=source,
        fixed_in_version=recorded_version,
        note=note,
        crash_id=crash_id,
    )
