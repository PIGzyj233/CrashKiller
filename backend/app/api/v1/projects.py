"""Projects REST API endpoints (/api/v1/projects)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import generate_api_key, get_db, hash_api_key
from app.db.base import utcnow
from app.db.models import Project
from app.schemas.project import (
    ProjectCreate,
    ProjectCreateResponse,
    ProjectRead,
    ProjectRotateKeyResponse,
    ProjectUpdate,
)

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    session: AsyncSession = Depends(get_db),
) -> ProjectCreateResponse:
    existing = (
        await session.execute(select(Project).where(Project.key == payload.key))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Project with key {payload.key!r} already exists",
        )

    raw_api_key = generate_api_key()
    project = Project(
        key=payload.key,
        name=payload.name,
        api_key_hash=hash_api_key(raw_api_key),
        settings_json=dict(payload.settings_json or {}),
        created_at=utcnow(),
    )
    session.add(project)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Project with key {payload.key!r} already exists",
        ) from exc
    await session.refresh(project)

    return ProjectCreateResponse(
        id=project.id,
        key=project.key,
        name=project.name,
        settings_json=dict(project.settings_json or {}),
        created_at=project.created_at,
        api_key=raw_api_key,
    )


@router.get("", response_model=list[ProjectRead])
async def list_projects(
    session: AsyncSession = Depends(get_db),
) -> list[ProjectRead]:
    rows = (
        await session.execute(select(Project).order_by(Project.created_at.asc()))
    ).scalars().all()
    return [ProjectRead.model_validate(row) for row in rows]


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> ProjectRead:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return ProjectRead.model_validate(project)


@router.patch("/{project_id}", response_model=ProjectRead)
async def update_project(
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    session: AsyncSession = Depends(get_db),
) -> ProjectRead:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    if payload.name is not None:
        project.name = payload.name
    if payload.settings_json is not None:
        project.settings_json = dict(payload.settings_json)

    await session.commit()
    await session.refresh(project)
    return ProjectRead.model_validate(project)


@router.post("/{project_id}/rotate-key", response_model=ProjectRotateKeyResponse)
async def rotate_project_key(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> ProjectRotateKeyResponse:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    new_key = generate_api_key()
    project.api_key_hash = hash_api_key(new_key)
    await session.commit()
    await session.refresh(project)

    return ProjectRotateKeyResponse(
        id=project.id,
        key=project.key,
        api_key=new_key,
    )
