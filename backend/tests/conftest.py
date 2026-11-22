"""Shared pytest fixtures for CrashKiller backend M1 unit tests."""

from __future__ import annotations

from collections import namedtuple
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.base import Base
from app.db.session import create_async_engine_from_url
from app.services.storage import GIB, StorageManager

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


def make_disk_usage_fn(free_gb: float, total_gb: float = 500.0) -> Callable[[Path | str], Any]:
    """Return a fake shutil.disk_usage callable reporting a deterministic free space."""
    total_bytes = int(total_gb * GIB)
    free_bytes = int(free_gb * GIB)
    used_bytes = max(0, total_bytes - free_bytes)

    def _fake_usage(_path: Path | str) -> DiskUsage:
        return DiskUsage(total=total_bytes, used=used_bytes, free=free_bytes)

    return _fake_usage


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


@pytest.fixture
def test_settings(tmp_data_dir: Path) -> Settings:
    return Settings(
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        DATA_DIR=str(tmp_data_dir),
        CRASHKILLER_DATA_PATH=str(tmp_data_dir),
        ENABLE_PUBLIC_SYMBOLS=True,
        ARCHIVE_RAW_SYMBOLS=False,
    )


@pytest.fixture
async def async_engine() -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine_from_url("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(async_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(
        bind=async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with factory() as session:
        yield session


@pytest.fixture
def storage_manager(tmp_data_dir: Path, test_settings: Settings) -> StorageManager:
    return StorageManager(
        data_dir=tmp_data_dir,
        settings=test_settings,
        disk_usage_fn=make_disk_usage_fn(free_gb=200.0),
    )


def make_mock_exec(
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    calls_log: list[tuple[str, ...]] | None = None,
    delay: float = 0.0,
) -> Callable[..., Any]:
    """Create a mock asyncio.create_subprocess_exec factory for unit tests."""
    import asyncio

    class _FakeProc:
        def __init__(self) -> None:
            self.returncode = returncode
            self.killed = False

        async def communicate(self) -> tuple[bytes, bytes]:
            if delay > 0:
                await asyncio.sleep(delay)
            return stdout, stderr

        def kill(self) -> None:
            self.killed = True

    async def _fake_exec(*cmd: str, **_kwargs: Any) -> _FakeProc:
        if calls_log is not None:
            calls_log.append(tuple(cmd))
        return _FakeProc()

    return _fake_exec
