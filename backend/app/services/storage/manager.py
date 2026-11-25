"""StorageManager facade combining POSIX I/O, quota reporting, and TTL/LRU GC."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.services.storage.gc import run_storage_gc
from app.services.storage.io_ops import StorageIOMixin
from app.services.storage.quota import StorageQuotaMixin


class StorageManager(StorageIOMixin, StorageQuotaMixin):
    """Manages CrashKiller's /data partition layout, atomic zstd I/O, quotas, and GC."""

    def __init__(
        self,
        data_dir: Path | str,
        settings: Settings,
        disk_usage_fn: Callable[[Path | str], Any] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.settings = settings
        self.disk_usage_fn = disk_usage_fn
        self.ensure_directories()

    async def run_gc(
        self,
        session: AsyncSession,
        force_emergency: bool = False,
    ) -> dict[str, Any]:
        """Run TTL + LRU garbage collection while protecting pinned crashes & issues."""
        async with self.shared_lock("capacity"):
            return await self._run_gc(session, force_emergency)

    async def _run_gc(
        self, session: AsyncSession, force_emergency: bool
    ) -> dict[str, Any]:
        return await run_storage_gc(self, session, force_emergency=force_emergency)
