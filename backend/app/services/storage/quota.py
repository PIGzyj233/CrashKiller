"""Partition disk usage accounting, quota checks, and host safety-floor reporting."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.storage.constants import GIB, PARTITIONS, StorageCapacityError, StorageError

if TYPE_CHECKING:
    from app.config import Settings


class StorageQuotaMixin:
    """Disk usage accounting, quota admission checks, and age-sorted file scans."""

    data_dir: Path
    settings: Settings
    disk_usage_fn: Callable[[Path | str], Any] | None

    if TYPE_CHECKING:

        def ensure_directories(self) -> None: ...

        def partition_path(self, partition: str) -> Path: ...

    async def require_capacity(
        self, session: AsyncSession, additional_bytes: Mapping[str, int]
    ) -> None:
        """Check application quotas under the shared capacity lock."""
        db_used = await self.get_db_size_bytes(session)
        if db_used and db_used >= self.get_partition_quota_bytes("db"):
            raise StorageCapacityError("PostgreSQL storage quota reached")
        sizes = await asyncio.to_thread(
            lambda: {part: self.partition_used_bytes(part)[0] for part in PARTITIONS}
        )
        for partition, incoming in additional_bytes.items():
            if sizes[partition] + incoming > self.get_partition_quota_bytes(partition):
                raise StorageCapacityError(f"Storage quota reached for {partition}")
        if (
            db_used + sum(sizes.values()) + sum(additional_bytes.values())
            > int(self.settings.TOTAL_DISK_QUOTA_GB * GIB)
        ):
            raise StorageCapacityError("CrashKiller total storage quota reached")

    def partition_used_bytes(self, partition: str) -> tuple[int, int]:
        """Return (total_bytes, file_count) for a partition, ignoring temp files."""
        root = self.partition_path(partition)
        total_bytes = 0
        file_count = 0
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                if entry.name.startswith(".tmp_"):
                                    continue
                                total_bytes += entry.stat(follow_symlinks=False).st_size
                                file_count += 1
                        except FileNotFoundError:
                            continue
            except FileNotFoundError:
                continue
        return total_bytes, file_count

    def get_partition_quota_bytes(self, partition: str) -> int:
        """Return configured hard quota in bytes for a partition."""
        mapping = {
            "db": self.settings.DB_QUOTA_GB,
            "symbols/sym": self.settings.SYMBOLS_SYM_QUOTA_GB,
            "symbols/public_cache": self.settings.SYMBOLS_PUBLIC_CACHE_QUOTA_GB,
            "symbols/raw": self.settings.SYMBOLS_RAW_QUOTA_GB,
            "logs": self.settings.LOGS_QUOTA_GB,
            "dumps": self.settings.DUMPS_QUOTA_GB,
            "staging": self.settings.STAGING_QUOTA_GB,
        }
        gb = mapping.get(partition)
        if gb is None:
            raise StorageError(f"No quota configured for partition: {partition}")
        return int(gb * GIB)

    def is_host_floor_breached(self) -> bool:
        """Return True when host free space drops below HOST_SAFETY_FLOOR_GB."""
        self.ensure_directories()
        fn = self.disk_usage_fn or shutil.disk_usage
        usage = fn(self.data_dir)
        host_free_gb = usage.free / GIB
        return host_free_gb < self.settings.HOST_SAFETY_FLOOR_GB

    def get_disk_usage_report(self, db_used_bytes: int = 0) -> dict[str, Any]:
        """Return per-partition usage, total quota consumption, and host floor status."""
        self.ensure_directories()
        partitions_report: dict[str, dict[str, Any]] = {}
        fs_used_total = 0

        for part in PARTITIONS:
            used_bytes, file_count = self.partition_used_bytes(part)
            quota_bytes = self.get_partition_quota_bytes(part)
            usage_pct = round((used_bytes / quota_bytes) * 100.0, 2) if quota_bytes > 0 else 0.0
            partitions_report[part] = {
                "used_bytes": used_bytes,
                "quota_bytes": quota_bytes,
                "usage_percent": usage_pct,
                "file_count": file_count,
            }
            fs_used_total += used_bytes

        db_quota = self.get_partition_quota_bytes("db")
        partitions_report["db"] = {
            "used_bytes": db_used_bytes,
            "quota_bytes": db_quota,
            "usage_percent": round((db_used_bytes / db_quota) * 100.0, 2) if db_quota > 0 else 0.0,
            "file_count": 0,
        }

        total_used_bytes = fs_used_total + db_used_bytes
        total_quota_bytes = int(self.settings.TOTAL_DISK_QUOTA_GB * GIB)
        total_usage_percent = (
            round((total_used_bytes / total_quota_bytes) * 100.0, 2)
            if total_quota_bytes > 0
            else 0.0
        )

        fn = self.disk_usage_fn or shutil.disk_usage
        usage = fn(self.data_dir)
        host_free_gb = round(usage.free / GIB, 3)
        safety_floor_breached = (usage.free / GIB) < self.settings.HOST_SAFETY_FLOOR_GB

        return {
            "partitions": partitions_report,
            "total_used_bytes": total_used_bytes,
            "total_quota_bytes": total_quota_bytes,
            "total_usage_percent": total_usage_percent,
            "host_total_bytes": usage.total,
            "host_used_bytes": usage.used,
            "host_free_bytes": usage.free,
            "host_free_gb": host_free_gb,
            "host_safety_floor_gb": self.settings.HOST_SAFETY_FLOOR_GB,
            "safety_floor_breached": safety_floor_breached,
            "gc_threshold_percent": self.settings.GC_THRESHOLD_PERCENT,
        }

    async def get_db_size_bytes(self, session: AsyncSession) -> int:
        """Query PostgreSQL database size in bytes (returns 0 on SQLite)."""
        bind = session.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            result = await session.execute(text("SELECT pg_database_size(current_database())"))
            val = result.scalar_one_or_none()
            return int(val or 0)
        return 0

    def _iter_files_sorted_by_age(self, root: Path) -> list[tuple[float, Path, int]]:
        """Collect all non-temp files under root sorted oldest-first."""
        items: list[tuple[float, Path, int]] = []
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                if entry.name.startswith(".tmp_"):
                                    continue
                                st = entry.stat(follow_symlinks=False)
                                ts = max(st.st_atime, st.st_mtime)
                                items.append((ts, Path(entry.path), st.st_size))
                        except FileNotFoundError:
                            continue
            except FileNotFoundError:
                continue
        items.sort(key=lambda x: x[0])
        return items
