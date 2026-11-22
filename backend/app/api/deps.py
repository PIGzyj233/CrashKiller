"""FastAPI dependency injection helpers and upload API-key authentication."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from base64 import b64decode
from collections.abc import AsyncGenerator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security.utils import get_authorization_scheme_param
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import Project
from app.db.session import get_session_factory
from app.services import settings_service
from app.services.events import EventBus
from app.services.storage import StorageManager
from app.workers.worker import TaskDispatcher


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Yield an async SQLAlchemy session from app.state or the active session factory."""
    factory = getattr(request.app.state, "session_factory", None) or get_session_factory()
    async with factory() as session:
        yield session


def get_app_settings(request: Request) -> Settings:
    """Return app settings stored on app.state or fallback to get_settings()."""
    state_settings = getattr(request.app.state, "settings", None)
    return state_settings if state_settings is not None else get_settings()


async def require_admin(request: Request) -> None:
    """Authorize browser and management requests against deployment credentials."""
    settings = get_app_settings(request)
    username = settings.ADMIN_USERNAME
    password = settings.ADMIN_PASSWORD.get_secret_value()
    if not username.strip() or not password.strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Configure ADMIN_USERNAME and ADMIN_PASSWORD to enable admin access",
        )

    scheme, encoded = get_authorization_scheme_param(request.headers.get("Authorization"))
    if scheme.lower() == "basic":
        try:
            provided = b64decode(encoded.strip(), validate=True)
        except ValueError:
            pass
        else:
            expected = f"{username}:{password}".encode()
            if hmac.compare_digest(provided, expected):
                _check_admin_origin(request, settings)
                return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Admin authentication required",
        headers={"WWW-Authenticate": 'Basic realm="CrashKiller", charset="UTF-8"'},
    )


def _check_admin_origin(request: Request, settings: Settings) -> None:
    """Keep browser state changes within the app or explicitly trusted origins."""
    origin = request.headers.get("Origin")
    if not origin or request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    host = request.url.netloc
    if origin in {f"http://{host}", f"https://{host}"}:
        return
    if origin != "*" and origin in settings.CORS_ORIGINS:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Untrusted request origin")


async def require_symsrv_access(request: Request) -> None:
    if not get_app_settings(request).ALLOW_UNAUTHENTICATED_SYMSRV:
        await require_admin(request)


def get_storage(request: Request) -> StorageManager:
    """Return the StorageManager bound to app.state."""
    return request.app.state.storage


def get_event_bus_dep(request: Request) -> EventBus:
    """Return the EventBus bound to app.state."""
    return request.app.state.event_bus


def get_dispatcher(request: Request) -> TaskDispatcher:
    """Return the TaskDispatcher bound to app.state."""
    return request.app.state.dispatcher


def hash_api_key(raw_key: str) -> str:
    """Return lowercase SHA-256 hex digest of an API key."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    """Generate a high-entropy project API key."""
    return f"ck_{secrets.token_urlsafe(24)}"


async def get_effective_storage(
    session: AsyncSession,
    storage: StorageManager,
    base_settings: Settings,
) -> tuple[Settings, StorageManager]:
    """Resolve DB runtime settings overrides and return an effective StorageManager."""
    effective_app = await settings_service.get_effective_app_settings(
        session,
        base_settings=base_settings,
    )
    eff_storage = StorageManager(
        data_dir=storage.data_dir,
        settings=effective_app,
        disk_usage_fn=storage.disk_usage_fn,
    )
    return effective_app, eff_storage


async def verify_project_upload_auth(
    session: AsyncSession,
    project_key: str | None,
    project_id: str | None,
    header_api_key: str | None,
    form_api_key: str | None,
) -> Project:
    """Resolve Project by key/id and verify X-API-Key header or api_key form field."""
    pk = (project_key or "").strip()
    pid_str = (project_id or "").strip()

    if not pk and not pid_str:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Either project_key or project_id is required",
        )

    project: Project | None = None
    if pid_str:
        try:
            pid = uuid.UUID(pid_str)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Invalid project_id UUID",
            ) from exc
        project = await session.get(Project, pid)
        if project is not None and pk and project.key != pk:
            project = None
    else:
        stmt = select(Project).where(Project.key == pk)
        project = (await session.execute(stmt)).scalar_one_or_none()

    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    hdr = (header_api_key or "").strip()
    frm = (form_api_key or "").strip()
    if hdr and frm and not hmac.compare_digest(hdr, frm):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Conflicting API keys provided",
        )

    provided_key = hdr or frm
    if not provided_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key (provide X-API-Key header or api_key form field)",
        )

    if not hmac.compare_digest(hash_api_key(provided_key), project.api_key_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )

    return project


__all__ = [
    "Depends",
    "generate_api_key",
    "get_app_settings",
    "get_db",
    "get_dispatcher",
    "get_effective_storage",
    "get_event_bus_dep",
    "get_storage",
    "hash_api_key",
    "require_admin",
    "require_symsrv_access",
    "verify_project_upload_auth",
]
