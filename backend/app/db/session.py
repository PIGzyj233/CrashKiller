"""Async SQLAlchemy engine and session management for PostgreSQL and SQLite."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool

from app.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_active_database_url: str | None = None


def create_async_engine_from_url(database_url: str, echo: bool = False) -> AsyncEngine:
    """Create an AsyncEngine configured appropriately for SQLite or PostgreSQL."""
    if "sqlite" in database_url:
        kwargs: dict[str, Any] = {
            "echo": echo,
            "connect_args": {"check_same_thread": False, "timeout": 30},
        }
        if ":memory:" in database_url or "mode=memory" in database_url:
            kwargs["poolclass"] = StaticPool
        else:
            kwargs["poolclass"] = NullPool

        engine = create_async_engine(database_url, **kwargs)

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    return create_async_engine(
        database_url,
        echo=echo,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def init_db(database_url: str | None = None, echo: bool | None = None) -> AsyncEngine:
    """Initialize (or reinitialize) the module-level engine and session factory."""
    global _engine, _session_factory, _active_database_url
    settings = get_settings()
    url = database_url or settings.DATABASE_URL
    use_echo = settings.DEBUG if echo is None else echo

    _engine = create_async_engine_from_url(url, echo=use_echo)
    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    _active_database_url = url
    return _engine


def get_engine() -> AsyncEngine:
    """Return the active AsyncEngine, initializing lazily if needed."""
    settings = get_settings()
    if _engine is None or _active_database_url != settings.DATABASE_URL:
        init_db(settings.DATABASE_URL)
    assert _engine is not None
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the active async_sessionmaker, initializing lazily if needed."""
    get_engine()
    assert _session_factory is not None
    return _session_factory


async def dispose_engine() -> None:
    """Dispose the active engine and clear cached state."""
    global _engine, _session_factory, _active_database_url
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
    _active_database_url = None


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an AsyncSession."""
    factory = get_session_factory()
    async with factory() as session:
        yield session
