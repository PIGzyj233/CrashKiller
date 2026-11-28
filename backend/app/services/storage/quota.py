"""Partition disk usage accounting, quota checks, and host safety-floor reporting."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.storage.constants import GIB, PARTITIONS, StorageError

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

        @staticmethod
        def _staging_in_use(root: Path, workspace: str) -> bool: ...

    async def ensure_usage_ledger(
        self, session: AsyncSession, *, lock: bool = True
    ) -> None:
        """Ensure partition usage ledger rows are initialized before file mutations."""
        from app.services.storage.ledger import ensure_ledger_rows

        await ensure_ledger_rows(session, self, lock=lock)

    async def require_capacity(
        self, session: AsyncSession, additional_bytes: Mapping[str, int]
    ) -> None:
        """Check application quotas in O(1) via the storage partition usage ledger."""
        from app.services.storage.ledger import check_capacity_from_ledger

        await check_capacity_from_ledger(session, self, additional_bytes)

    async def commit_usage_deltas(
        self,
        session: AsyncSession,
        byte_deltas: Mapping[str, int],
        file_deltas: Mapping[str, int] | None = None,
        *,
        enforce_quota: bool = True,
        extra_headroom_checks: Mapping[str, int] | None = None,
        check_partitions: Iterable[str] | None = None,
    ) -> None:
        """Atomically verify quotas (when enforcing) and apply usage deltas in the ledger."""
        from app.services.storage.ledger import commit_partition_deltas

        await commit_partition_deltas(
            session,
            self,
            byte_deltas,
            file_deltas,
            enforce_quota=enforce_quota,
            extra_headroom_checks=extra_headroom_checks,
            check_partitions=check_partitions,
        )

    async def reconcile_usage(self, session: AsyncSession) -> dict[str, tuple[int, int]]:
        """Re-scan disk partitions and synchronize the database usage ledger."""
        from app.services.storage.ledger import reconcile_partition_usage

        return await reconcile_partition_usage(session, self)

    async def reconcile_partition_usage_single(
        self, session: AsyncSession, partition: str
    ) -> tuple[int, int]:
        """Re-scan a single disk partition and update its database usage ledger row."""
        from app.services.storage.ledger import reconcile_single_partition_usage

        return await reconcile_single_partition_usage(session, self, partition)

    async def reserve_staging_scratch(
        self,
        session: AsyncSession,
        staging_dir: Path,
        reserved_bytes: int,
    ) -> None:
        """Atomically reserve scratch budget inside an active staging_workspace() under lock."""
        from app.services.storage.ledger import reserve_staging_scratch_capacity

        await reserve_staging_scratch_capacity(session, self, staging_dir, reserved_bytes)

    async def get_disk_usage_report_async(
        self, session: AsyncSession, db_used_bytes: int = 0
    ) -> dict[str, Any]:
        """Return disk usage report from the O(1) database ledger plus host statvfs."""
        from app.services.storage.ledger import build_disk_usage_report_from_ledger

        return await build_disk_usage_report_from_ledger(
            session, self, db_used_bytes=db_used_bytes
        )

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

    def ledger_partition_used_bytes(self, partition: str) -> tuple[int, int]:
        """Scan persistent ledger-tracked bytes for a partition.

        For ``staging``, actively leased scratch workspaces (``staging_workspace()``) are
        excluded so reconciliation never persists ephemeral scratch bytes that are removed
        without ledger deltas when the workspace exits.
        """
        if partition != "staging":
            return self.partition_used_bytes(partition)
        root = self.partition_path("staging")
        total_bytes = 0
        file_count = 0
        stack: list[Path] = []
        try:
            with os.scandir(root) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if self._staging_in_use(root, entry.name):
                                continue
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            if entry.name.startswith(".tmp_"):
                                continue
                            total_bytes += entry.stat(follow_symlinks=False).st_size
                            file_count += 1
                    except FileNotFoundError:
                        continue
        except FileNotFoundError:
            return 0, 0

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

    def active_staging_scratch_bytes(self) -> tuple[int, int]:
        """Return live (bytes, files) inside active leased staging_workspace() directories."""
        root = self.partition_path("staging")
        active_dirs: list[Path] = []
        try:
            with os.scandir(root) as it:
                for entry in it:
                    name = entry.name
                    if not (name.startswith(".tmp_") and name.endswith(".lock")):
                        continue
                    workspace = name[len(".tmp_") : -len(".lock")]
                    if (
                        not workspace
                        or workspace.startswith("upload_")
                        or workspace.startswith("symupload_")
                    ):
                        continue
                    ws_dir = root / workspace
                    if ws_dir.is_dir() and self._staging_in_use(root, workspace):
                        active_dirs.append(ws_dir)
        except FileNotFoundError:
            return 0, 0

        if not active_dirs:
            return 0, 0

        total_bytes = 0
        file_count = 0
        for ws_dir in active_dirs:
            ws_bytes = 0
            ws_files = 0
            reserved = 0
            reserve_marker = ws_dir / ".tmp_reserved_bytes"
            if reserve_marker.is_file():
                try:
                    reserved = max(0, int(reserve_marker.read_text(encoding="utf-8").strip()))
                except (OSError, ValueError):
                    reserved = 0
            stack = [ws_dir]
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
                                    ws_bytes += entry.stat(follow_symlinks=False).st_size
                                    ws_files += 1
                            except FileNotFoundError:
                                continue
                except FileNotFoundError:
                    continue
            total_bytes += max(ws_bytes, reserved)
            file_count += ws_files
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

    def _format_disk_usage_report(
        self,
        partition_stats: Mapping[str, tuple[int, int]],
        *,
        db_used_bytes: int = 0,
    ) -> dict[str, Any]:
        partitions_report: dict[str, dict[str, Any]] = {}
        fs_used_total = 0

        for part in PARTITIONS:
            used_bytes, file_count = partition_stats.get(part, (0, 0))
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

    def get_disk_usage_report(self, db_used_bytes: int = 0) -> dict[str, Any]:
        """Return per-partition usage, total quota consumption, and host floor status."""
        self.ensure_directories()
        partition_stats = {part: self.partition_used_bytes(part) for part in PARTITIONS}
        return self._format_disk_usage_report(partition_stats, db_used_bytes=db_used_bytes)

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
