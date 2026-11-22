"""Issues & crash clusters REST API endpoints (/api/v1/issues)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.db.base import utcnow
from app.db.models import CrashCluster, CrashReport, Issue, Project
from app.schemas.crash import CrashSummary
from app.schemas.issue import (
    ClusterListResponse,
    CrashClusterRead,
    IssueCreateRequest,
    IssueDetailRead,
    IssueListResponse,
    IssueMergeRequest,
    IssueRead,
    IssueSplitRequest,
    IssueSplitResponse,
    IssueStatusLiteral,
    IssueUpdateRequest,
)

router = APIRouter(prefix="/issues", tags=["issues"])


def _as_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return utcnow()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


async def _recompute_issue_stats(session: AsyncSession, issue: Issue) -> None:
    count_stmt = select(func.count()).select_from(CrashReport).where(
        CrashReport.issue_id == issue.id
    )
    issue.crash_count = int((await session.execute(count_stmt)).scalar_one())

    bounds_stmt = select(
        func.min(CrashReport.crash_time),
        func.max(CrashReport.crash_time),
    ).where(CrashReport.issue_id == issue.id)
    min_ts, max_ts = (await session.execute(bounds_stmt)).one()
    if min_ts is not None:
        issue.first_seen_at = min_ts
    if max_ts is not None:
        issue.last_seen_at = max_ts


async def _load_issue_detail(session: AsyncSession, issue_id: int) -> IssueDetailRead:
    issue = await session.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Issue not found",
        )

    clusters = (
        await session.execute(
            select(CrashCluster)
            .where(CrashCluster.issue_id == issue_id)
            .order_by(CrashCluster.crash_count.desc())
        )
    ).scalars().all()

    recent_crashes = (
        await session.execute(
            select(CrashReport)
            .where(CrashReport.issue_id == issue_id)
            .order_by(CrashReport.created_at.desc())
            .limit(50)
        )
    ).scalars().all()

    issue_data = IssueRead.model_validate(issue).model_dump()
    return IssueDetailRead(
        **issue_data,
        clusters=[CrashClusterRead.model_validate(c) for c in clusters],
        recent_crashes=[CrashSummary.model_validate(cr) for cr in recent_crashes],
    )


@router.get("", response_model=IssueListResponse)
async def list_issues(
    project_id: uuid.UUID | None = None,
    status_filter: IssueStatusLiteral | None = Query(default=None, alias="status"),
    component: str | None = None,
    is_pinned: bool | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> IssueListResponse:
    filters = []
    if project_id is not None:
        filters.append(Issue.project_id == project_id)
    if status_filter is not None:
        filters.append(Issue.status == status_filter)
    if component is not None and component.strip():
        filters.append(Issue.component == component.strip())
    if is_pinned is not None:
        filters.append(Issue.is_pinned == is_pinned)
    if q is not None and q.strip():
        pattern = f"%{q.strip()}%"
        filters.append(
            or_(
                Issue.title.ilike(pattern),
                Issue.root_cause.ilike(pattern),
                Issue.component.ilike(pattern),
            )
        )

    count_stmt = select(func.count()).select_from(Issue)
    list_stmt = select(Issue)
    if filters:
        count_stmt = count_stmt.where(*filters)
        list_stmt = list_stmt.where(*filters)

    total = int((await session.execute(count_stmt)).scalar_one())
    rows = (
        await session.execute(
            list_stmt.order_by(Issue.last_seen_at.desc(), Issue.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()

    return IssueListResponse(
        items=[IssueRead.model_validate(row) for row in rows],
        total=total,
    )


@router.get("/clusters", response_model=ClusterListResponse)
async def list_clusters(
    project_id: uuid.UUID | None = None,
    issue_id: int | None = None,
    unassigned_only: bool = False,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> ClusterListResponse:
    filters = []
    if project_id is not None:
        filters.append(CrashCluster.project_id == project_id)
    if issue_id is not None:
        filters.append(CrashCluster.issue_id == issue_id)
    if unassigned_only:
        filters.append(CrashCluster.issue_id.is_(None))

    count_stmt = select(func.count()).select_from(CrashCluster)
    list_stmt = select(CrashCluster)
    if filters:
        count_stmt = count_stmt.where(*filters)
        list_stmt = list_stmt.where(*filters)

    total = int((await session.execute(count_stmt)).scalar_one())
    rows = (
        await session.execute(
            list_stmt.order_by(CrashCluster.crash_count.desc()).offset(offset).limit(limit)
        )
    ).scalars().all()
    return ClusterListResponse(
        items=[CrashClusterRead.model_validate(row) for row in rows],
        total=total,
    )


@router.post("/merge", response_model=IssueDetailRead)
async def merge_issues(
    payload: IssueMergeRequest,
    session: AsyncSession = Depends(get_db),
) -> IssueDetailRead:
    source_ids = list(dict.fromkeys(payload.source_issue_ids))
    if not source_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="source_issue_ids must not be empty",
        )
    if payload.target_issue_id in source_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot merge an issue into itself",
        )

    target = await session.get(Issue, payload.target_issue_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target issue not found",
        )

    sources = (
        await session.execute(select(Issue).where(Issue.id.in_(source_ids)))
    ).scalars().all()
    if len(sources) != len(source_ids):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="One or more source issues not found",
        )

    for src in sources:
        if src.project_id != target.project_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot merge issues from different projects",
            )

    merged_patterns = list(target.log_patterns_json or [])
    target_first = _as_utc(target.first_seen_at)
    target_last = _as_utc(target.last_seen_at)
    for src in sources:
        for pat in src.log_patterns_json or []:
            if pat not in merged_patterns:
                merged_patterns.append(pat)
        src_first = _as_utc(src.first_seen_at)
        src_last = _as_utc(src.last_seen_at)
        if src_first < target_first:
            target_first = src_first
            target.first_seen_at = src_first
        if src_last > target_last:
            target_last = src_last
            target.last_seen_at = src_last
        if not target.root_cause and src.root_cause:
            target.root_cause = src.root_cause
        if not target.component and src.component:
            target.component = src.component
        if src.is_pinned:
            target.is_pinned = True

    target.log_patterns_json = merged_patterns
    await session.flush()

    await session.execute(
        update(CrashCluster)
        .where(CrashCluster.issue_id.in_(source_ids))
        .values(issue_id=target.id)
    )
    await session.execute(
        update(CrashReport)
        .where(CrashReport.issue_id.in_(source_ids))
        .values(issue_id=target.id)
    )
    await session.execute(delete(Issue).where(Issue.id.in_(source_ids)))
    session.expire_all()

    target = await session.get(Issue, payload.target_issue_id)
    assert target is not None
    await _recompute_issue_stats(session, target)
    await session.commit()
    return await _load_issue_detail(session, target.id)


@router.post("", response_model=IssueRead, status_code=status.HTTP_201_CREATED)
async def create_issue(
    payload: IssueCreateRequest,
    session: AsyncSession = Depends(get_db),
) -> IssueRead:
    project = await session.get(Project, payload.project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    now = utcnow()
    cleaned_patterns = list(
        dict.fromkeys(p.strip() for p in payload.log_patterns_json if p and p.strip())
    )
    issue = Issue(
        project_id=payload.project_id,
        title=payload.title,
        status=payload.status,
        component=(
            payload.component.strip()
            if payload.component and payload.component.strip()
            else None
        ),
        root_cause=payload.root_cause,
        log_patterns_json=cleaned_patterns,
        fixed_in_version=(
            payload.fixed_in_version.strip()
            if payload.fixed_in_version and payload.fixed_in_version.strip()
            else None
        ),
        is_ambiguous_stack=payload.is_ambiguous_stack,
        is_pinned=payload.is_pinned,
        crash_count=0,
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(issue)
    await session.commit()
    await session.refresh(issue)
    return IssueRead.model_validate(issue)


@router.get("/{issue_id}", response_model=IssueDetailRead)
async def get_issue(
    issue_id: int,
    session: AsyncSession = Depends(get_db),
) -> IssueDetailRead:
    return await _load_issue_detail(session, issue_id)


@router.patch("/{issue_id}", response_model=IssueRead)
async def update_issue(
    issue_id: int,
    payload: IssueUpdateRequest,
    session: AsyncSession = Depends(get_db),
) -> IssueRead:
    issue = await session.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Issue not found",
        )

    updates = payload.model_dump(exclude_unset=True)
    if "title" in updates and updates["title"] is not None:
        issue.title = updates["title"]
    if "status" in updates and updates["status"] is not None:
        issue.status = updates["status"]
    if "component" in updates:
        comp = updates["component"]
        issue.component = comp.strip() if comp and str(comp).strip() else None
    if "root_cause" in updates:
        issue.root_cause = updates["root_cause"]
    if "log_patterns_json" in updates and updates["log_patterns_json"] is not None:
        issue.log_patterns_json = list(
            dict.fromkeys(p.strip() for p in updates["log_patterns_json"] if p and p.strip())
        )
    if "fixed_in_version" in updates:
        fv = updates["fixed_in_version"]
        issue.fixed_in_version = fv.strip() if fv and str(fv).strip() else None
    if "is_ambiguous_stack" in updates and updates["is_ambiguous_stack"] is not None:
        issue.is_ambiguous_stack = bool(updates["is_ambiguous_stack"])
    if "is_pinned" in updates and updates["is_pinned"] is not None:
        issue.is_pinned = bool(updates["is_pinned"])

    await session.commit()
    await session.refresh(issue)
    return IssueRead.model_validate(issue)


@router.post("/{issue_id}/split", response_model=IssueSplitResponse)
async def split_issue_cluster(
    issue_id: int,
    payload: IssueSplitRequest,
    session: AsyncSession = Depends(get_db),
) -> IssueSplitResponse:
    source_issue = await session.get(Issue, issue_id)
    if source_issue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source issue not found",
        )

    cluster = await session.get(CrashCluster, payload.cluster_id)
    if cluster is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cluster not found",
        )

    if cluster.issue_id != source_issue.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cluster does not belong to the specified source issue",
        )

    target_issue: Issue | None = None
    if payload.target_issue_id is not None:
        if payload.target_issue_id == source_issue.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="target_issue_id must differ from source issue",
            )
        target_issue = await session.get(Issue, payload.target_issue_id)
        if target_issue is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Target issue not found",
            )
        if target_issue.project_id != source_issue.project_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Target issue belongs to a different project",
            )
    else:
        title = (payload.new_issue_title or "").strip()
        if not title:
            sig_preview = (cluster.signature_text or cluster.signature_hash)[:80]
            title = f"Split from #{source_issue.id}: {sig_preview}"
        now = utcnow()
        target_issue = Issue(
            project_id=source_issue.project_id,
            title=title[:512],
            status="NEW",
            component=source_issue.component,
            crash_count=0,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(target_issue)
        await session.flush()

    cluster.issue_id = target_issue.id
    crashes = (
        await session.execute(
            select(CrashReport).where(
                CrashReport.cluster_id == cluster.id,
                or_(
                    CrashReport.issue_id == source_issue.id,
                    CrashReport.issue_id.is_(None),
                ),
            )
        )
    ).scalars().all()
    for crash in crashes:
        crash.issue_id = target_issue.id

    await session.flush()
    await _recompute_issue_stats(session, source_issue)
    await _recompute_issue_stats(session, target_issue)
    await session.commit()

    await session.refresh(source_issue)
    await session.refresh(target_issue)
    await session.refresh(cluster)

    return IssueSplitResponse(
        source_issue=IssueRead.model_validate(source_issue),
        target_issue=IssueRead.model_validate(target_issue),
        cluster=CrashClusterRead.model_validate(cluster),
    )
