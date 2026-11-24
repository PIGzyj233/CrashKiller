"""POSIX storage abstraction, disk quota reporting, safety floor, and TTL/LRU GC."""

from __future__ import annotations

import asyncio
import fcntl
import os
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

import zstandard as zstd
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import CrashLog, CrashReport, Issue, Project, Symbol

GIB = 1024**3

PARTITIONS: tuple[str, ...] = (
    "symbols/sym",
    "symbols/public_cache",
    "symbols/raw",
    "logs",
    "dumps",
    "staging",
)
ACTIVE_CRASH_STATUSES = ("PENDING", "PARSING", "TRIAGING")


class StorageError(Exception):
    """Raised on invalid storage paths, traversal attempts, or I/O failures."""


class StorageCapacityError(StorageError):
    """Raised when an artifact cannot be admitted within its storage budget."""


class StorageManager:
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

    def ensure_directories(self) -> None:
        """Ensure the root data directory and all 6 partitions exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for part in PARTITIONS:
            (self.data_dir / part).mkdir(parents=True, exist_ok=True)

    def partition_path(self, partition: str) -> Path:
        """Return absolute path for a named partition, creating it if needed."""
        if partition not in PARTITIONS:
            raise StorageError(f"Unknown storage partition: {partition}")
        path = self.data_dir / partition
        path.mkdir(parents=True, exist_ok=True)
        return path

    @asynccontextmanager
    async def shared_lock(self, purpose: str) -> AsyncIterator[None]:
        """Serialize storage operations across the API and workers sharing DATA_DIR."""
        with (self.data_dir / f".{purpose}.lock").open("ab") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    await asyncio.sleep(0.025)
                else:
                    break
            yield

    @contextmanager
    def staging_workspace(self, prefix: str) -> Iterator[Path]:
        """Give a conversion a staging directory that GC cannot evict while in use."""
        root = self.partition_path("staging")
        workspace = root / f"{prefix}_{uuid.uuid4().hex}"
        marker = root / f".tmp_{workspace.name}.lock"
        with marker.open("xb") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX)
            try:
                workspace.mkdir()
                yield workspace
            finally:
                shutil.rmtree(workspace, ignore_errors=True)
                marker.unlink(missing_ok=True)

    @staticmethod
    def _staging_in_use(root: Path, workspace: str) -> bool:
        try:
            with (root / f".tmp_{workspace}.lock").open("rb") as lease:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except FileNotFoundError:
            return False
        except BlockingIOError:
            return True
        return False

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

    def resolve_path(self, rel_path: str | Path) -> Path:
        """Resolve a relative path safely inside data_dir, preventing traversal."""
        rel_str = str(rel_path)
        if "\x00" in rel_str:
            raise StorageError(f"NUL byte in path is not allowed: {rel_path!r}")
        if not rel_str.strip() or rel_str.strip() == ".":
            raise StorageError(f"Empty or root relative path is not allowed: {rel_path!r}")
        candidate = Path(rel_path)
        if candidate.is_absolute():
            raise StorageError(f"Absolute paths are not allowed: {rel_path}")
        root = self.data_dir.resolve()
        resolved = (root / candidate).resolve()
        if not resolved.is_relative_to(root) or resolved == root:
            raise StorageError(f"Path traversal detected: {rel_path}")
        return resolved

    def _safe_unlink(self, path: Path) -> int:
        """Delete a file if it exists and return bytes freed."""
        try:
            size = path.stat().st_size
            path.unlink()
            return size
        except FileNotFoundError:
            return 0

    def _prune_empty_dirs(self, start_dir: Path, stop_at: Path) -> None:
        """Remove empty parent directories up to (but not including) stop_at."""
        root = stop_at.resolve()
        current = start_dir.resolve()
        while current != root and current.is_relative_to(root):
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    def atomic_write_bytes(self, rel_path: str | Path, data: bytes) -> Path:
        """Atomically write bytes to rel_path inside data_dir."""
        dest = self.resolve_path(rel_path)
        if dest.is_dir():
            raise StorageError(f"Target path is a directory: {rel_path}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=dest.parent,
                prefix=".tmp_",
                delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                tmp.write(data)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, dest)
            tmp_path = None
            return dest
        finally:
            if tmp_path is not None:
                self._safe_unlink(tmp_path)

    def atomic_write_stream(
        self,
        rel_path: str | Path,
        stream: BinaryIO,
        chunk_size: int = 65536,
    ) -> tuple[Path, int]:
        """Atomically write a binary stream to rel_path, returning (path, bytes_written)."""
        dest = self.resolve_path(rel_path)
        if dest.is_dir():
            raise StorageError(f"Target path is a directory: {rel_path}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        total_bytes = 0
        try:
            with tempfile.NamedTemporaryFile(
                dir=dest.parent,
                prefix=".tmp_",
                delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                while True:
                    chunk = stream.read(chunk_size)
                    if not chunk:
                        break
                    tmp.write(chunk)
                    total_bytes += len(chunk)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, dest)
            tmp_path = None
            return dest, total_bytes
        finally:
            if tmp_path is not None:
                self._safe_unlink(tmp_path)

    def compress_dump(
        self,
        src_path_or_stream: Path | str | BinaryIO,
        crash_id: uuid.UUID | str,
    ) -> tuple[str, int, int]:
        """Compress a minidump to dumps/<crash_id>/<crash_id>.dmp.zst atomically."""
        if isinstance(src_path_or_stream, (str, Path)) and "\x00" in str(src_path_or_stream):
            raise StorageError(f"NUL byte in source path is not allowed: {src_path_or_stream!r}")
        crash_id_str = str(crash_id)
        if (
            not crash_id_str.strip()
            or crash_id_str.strip() in {".", ".."}
            or "/" in crash_id_str
            or "\\" in crash_id_str
            or "\x00" in crash_id_str
            or ".." in crash_id_str
        ):
            raise StorageError(f"Invalid crash_id path segment: {crash_id!r}")
        rel_path = f"dumps/{crash_id_str}/{crash_id_str}.dmp.zst"
        dest = self.resolve_path(rel_path)
        dumps_root = self.partition_path("dumps").resolve()
        if not dest.is_relative_to(dumps_root) or dest == dumps_root:
            raise StorageError(f"Dump destination escapes dumps partition: {crash_id!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        raw_size = 0
        compressor = zstd.ZstdCompressor(level=self.settings.DUMP_ZSTD_LEVEL)

        def _copy(src_fh: BinaryIO, dst_fh: BinaryIO) -> None:
            nonlocal raw_size
            with compressor.stream_writer(dst_fh, closefd=False) as zw:
                while True:
                    buf = src_fh.read(65536)
                    if not buf:
                        break
                    raw_size += len(buf)
                    zw.write(buf)
            dst_fh.flush()
            os.fsync(dst_fh.fileno())

        try:
            with tempfile.NamedTemporaryFile(
                dir=dest.parent,
                prefix=".tmp_",
                delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                if isinstance(src_path_or_stream, (str, Path)):
                    with open(src_path_or_stream, "rb") as src_fh:
                        _copy(src_fh, tmp)
                else:
                    _copy(src_path_or_stream, tmp)
            compressed_size = tmp_path.stat().st_size
            os.replace(tmp_path, dest)
            tmp_path = None
            return rel_path, raw_size, compressed_size
        finally:
            if tmp_path is not None:
                self._safe_unlink(tmp_path)

    def decompress_dump(self, rel_dump_path: str | Path, dest_path: Path | str) -> Path:
        """Stream-decompress a .dmp.zst archive to dest_path."""
        if "\x00" in str(dest_path):
            raise StorageError(f"NUL byte in destination path is not allowed: {dest_path!r}")
        src = self.resolve_path(rel_dump_path)
        if not src.exists():
            raise StorageError(f"Dump archive not found: {rel_dump_path}")
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        decompressor = zstd.ZstdDecompressor()
        try:
            with tempfile.NamedTemporaryFile(
                dir=dest.parent,
                prefix=".tmp_",
                delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                with open(src, "rb") as src_fh:
                    decompressor.copy_stream(src_fh, tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, dest)
            tmp_path = None
            return dest
        finally:
            if tmp_path is not None:
                self._safe_unlink(tmp_path)

    def compress_raw_symbol(
        self,
        src_path: Path | str,
        rel_dest_path: str | Path,
    ) -> tuple[str, int] | None:
        """Stream-compress a raw symbol file to symbols/raw unless host safety floor breached."""
        if "\x00" in str(src_path):
            raise StorageError(f"NUL byte in source path is not allowed: {src_path!r}")
        if self.is_host_floor_breached():
            return None
        dest = self.resolve_path(rel_dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        compressor = zstd.ZstdCompressor(level=self.settings.DUMP_ZSTD_LEVEL)
        try:
            with tempfile.NamedTemporaryFile(
                dir=dest.parent,
                prefix=".tmp_",
                delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                with open(src_path, "rb") as src_fh:
                    compressor.copy_stream(src_fh, tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
            compressed_size = tmp_path.stat().st_size
            os.replace(tmp_path, dest)
            tmp_path = None
            return str(rel_dest_path), compressed_size
        finally:
            if tmp_path is not None:
                self._safe_unlink(tmp_path)

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
        self.ensure_directories()
        now = datetime.now(UTC)
        emergency = force_emergency or self.is_host_floor_breached()
        threshold_ratio = self.settings.GC_THRESHOLD_PERCENT / 100.0

        def needs_emergency_reclaim() -> bool:
            return force_emergency or (emergency and self.is_host_floor_breached())

        freed_bytes = 0
        deleted_files_count = 0
        skipped_pinned_dumps: set[uuid.UUID] = set()
        skipped_pinned_logs: set[uuid.UUID] = set()

        # ------------------------------------------------------------------
        # Step 1: staging TTL (and emergency / quota cleanup)
        # ------------------------------------------------------------------
        staging_root = self.partition_path("staging")
        staging_cutoff_ts = (now - timedelta(hours=self.settings.STAGING_TTL_HOURS)).timestamp()
        staging_files = self._iter_files_sorted_by_age(staging_root)
        staging_used = sum(sz for _, _, sz in staging_files)
        staging_target = int(self.get_partition_quota_bytes("staging") * threshold_ratio)

        for ts, path, size in staging_files:
            workspace = path.relative_to(staging_root).parts[0]
            if self._staging_in_use(staging_root, workspace):
                continue
            if ts < staging_cutoff_ts or needs_emergency_reclaim() or staging_used > staging_target:
                removed = self._safe_unlink(path)
                if removed > 0 or not path.exists():
                    freed_bytes += removed
                    deleted_files_count += 1
                    staging_used = max(0, staging_used - size)
                    self._prune_empty_dirs(path.parent, staging_root)

        # ------------------------------------------------------------------
        # Step 2: TTL eviction for dumps & logs (protecting pinned items)
        # ------------------------------------------------------------------
        from app.services.settings_service import (
            default_effective_settings,
            resolve_project_settings,
        )

        effective_baseline = default_effective_settings(self.settings)
        projects = (await session.execute(select(Project))).scalars().all()
        retention_by_project: dict[uuid.UUID, tuple[datetime, datetime]] = {}
        for proj in projects:
            resolved = resolve_project_settings(effective_baseline, proj.settings_json)
            retention_by_project[proj.id] = (
                now - timedelta(days=int(resolved["dump_retention_days"])),
                now - timedelta(days=int(resolved["log_retention_days"])),
            )
        default_dump_cutoff = now - timedelta(days=self.settings.DUMP_RETENTION_DAYS)
        default_log_cutoff = now - timedelta(days=self.settings.LOG_RETENTION_DAYS)

        def _as_utc(dt: datetime) -> datetime:
            return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)

        dumps_root = self.partition_path("dumps")
        candidate_dumps_stmt = (
            select(CrashReport, Issue)
            .outerjoin(Issue, CrashReport.issue_id == Issue.id)
            .where(CrashReport.dump_path.is_not(None))
            .order_by(CrashReport.created_at.asc())
        )
        for report, issue in (await session.execute(candidate_dumps_stmt)).all():
            if report.status in ACTIVE_CRASH_STATUSES:
                continue
            dump_cutoff = retention_by_project.get(
                report.project_id,
                (default_dump_cutoff, default_log_cutoff),
            )[0]
            if _as_utc(report.created_at) >= dump_cutoff:
                continue
            if report.is_pinned or (issue is not None and issue.is_pinned):
                skipped_pinned_dumps.add(report.id)
                continue
            if report.dump_path:
                try:
                    target = self.resolve_path(report.dump_path)
                    removed = self._safe_unlink(target)
                    if removed > 0:
                        freed_bytes += removed
                        deleted_files_count += 1
                    self._prune_empty_dirs(target.parent, dumps_root)
                except StorageError:
                    pass
                report.dump_path = None

        logs_root = self.partition_path("logs")
        candidate_logs_stmt = (
            select(CrashLog, CrashReport, Issue)
            .join(CrashReport, CrashLog.crash_id == CrashReport.id)
            .outerjoin(Issue, CrashReport.issue_id == Issue.id)
            .where(CrashLog.zst_path.is_not(None))
            .order_by(CrashReport.created_at.asc())
        )
        for log_row, report, issue in (await session.execute(candidate_logs_stmt)).all():
            if report.status in ACTIVE_CRASH_STATUSES:
                continue
            log_cutoff = retention_by_project.get(
                report.project_id,
                (default_dump_cutoff, default_log_cutoff),
            )[1]
            if _as_utc(report.created_at) >= log_cutoff:
                continue
            if report.is_pinned or (issue is not None and issue.is_pinned):
                skipped_pinned_logs.add(log_row.id)
                continue
            for rel_attr in (log_row.zst_path, log_row.idx_path):
                if rel_attr:
                    try:
                        target = self.resolve_path(rel_attr)
                        removed = self._safe_unlink(target)
                        if removed > 0:
                            freed_bytes += removed
                            deleted_files_count += 1
                        self._prune_empty_dirs(target.parent, logs_root)
                    except StorageError:
                        pass
            log_row.zst_path = None
            log_row.idx_path = None

        await session.flush()

        # ------------------------------------------------------------------
        # Step 3: Bounded single-pass quota / emergency LRU eviction
        # ------------------------------------------------------------------
        # 3a. symbols/public_cache (evict miss cache -> untracked/auto-synced -> manual uploads)
        pub_root = self.partition_path("symbols/public_cache")
        pub_used, _ = self.partition_used_bytes("symbols/public_cache")
        pub_target = int(self.get_partition_quota_bytes("symbols/public_cache") * threshold_ratio)
        if pub_used > pub_target or needs_emergency_reclaim():
            pub_syms_stmt = select(Symbol).where(Symbol.project_id.is_(None))
            pub_syms = (await session.execute(pub_syms_stmt)).scalars().all()
            pub_by_path: dict[str, Symbol] = {s.sym_path: s for s in pub_syms if s.sym_path}
            data_root = self.data_dir.resolve()

            eviction_entries: list[tuple[int, float, Path, int, Symbol | None]] = []
            for mtime, path, size in self._iter_files_sorted_by_age(pub_root):
                if path.name == "_msdl_circuit.json":
                    continue
                try:
                    rel_path = path.resolve().relative_to(data_root).as_posix()
                except (OSError, ValueError):
                    rel_path = ""
                sym_row = pub_by_path.get(rel_path)
                if "/_msdl_miss/" in f"/{rel_path}":
                    tier = 0
                    sort_ts = mtime
                elif sym_row is None:
                    tier = 1
                    sort_ts = mtime
                elif sym_row.source != "upload":
                    tier = 2
                    sort_ts = _as_utc(sym_row.last_used_at).timestamp()
                else:
                    tier = 3
                    sort_ts = _as_utc(sym_row.last_used_at).timestamp()
                eviction_entries.append((tier, sort_ts, path, size, sym_row))

            eviction_entries.sort(key=lambda item: (item[0], item[1]))
            for _, _, path, size, sym_row in eviction_entries:
                if pub_used <= pub_target and not needs_emergency_reclaim():
                    break
                removed = self._safe_unlink(path)
                if removed > 0:
                    freed_bytes += removed
                    deleted_files_count += 1
                    pub_used = max(0, pub_used - size)
                    self._prune_empty_dirs(path.parent, pub_root)
                if sym_row is not None:
                    await session.delete(sym_row)

        # 3b. symbols/raw (LRU by Symbol.last_used_at, preserving .sym)
        raw_root = self.partition_path("symbols/raw")
        raw_used, _ = self.partition_used_bytes("symbols/raw")
        raw_target = int(self.get_partition_quota_bytes("symbols/raw") * threshold_ratio)
        if raw_used > raw_target or needs_emergency_reclaim():
            raw_syms_stmt = (
                select(Symbol)
                .where(Symbol.raw_archive_path.is_not(None))
                .order_by(Symbol.last_used_at.asc())
            )
            raw_syms = (await session.execute(raw_syms_stmt)).scalars().all()
            raw_counts: dict[str, int] = {}
            pe_counts: dict[tuple[str, str], int] = {}
            for s in raw_syms:
                if s.raw_archive_path:
                    raw_counts[s.raw_archive_path] = raw_counts.get(s.raw_archive_path, 0) + 1
                if s.code_id and s.code_file and s.has_pe_unwind:
                    pe_key = (s.code_id, s.code_file)
                    pe_counts[pe_key] = pe_counts.get(pe_key, 0) + 1

            for sym in raw_syms:
                if raw_used <= raw_target and not needs_emergency_reclaim():
                    break
                orig_raw_rel = sym.raw_archive_path
                pe_raw_rel: str | None = None
                if sym.code_id and sym.code_file and sym.has_pe_unwind:
                    pe_key = (sym.code_id, sym.code_file)
                    pe_counts[pe_key] = pe_counts.get(pe_key, 1) - 1
                    pe_raw_rel = f"symbols/raw/{sym.code_id}/{sym.code_file}.zst"

                if orig_raw_rel:
                    raw_counts[orig_raw_rel] = raw_counts.get(orig_raw_rel, 1) - 1
                    pe_still_referenced = (
                        pe_raw_rel is not None
                        and orig_raw_rel == pe_raw_rel
                        and sym.code_id is not None
                        and sym.code_file is not None
                        and pe_counts.get((sym.code_id, sym.code_file), 0) > 0
                    )
                    if raw_counts[orig_raw_rel] <= 0 and not pe_still_referenced:
                        try:
                            target = self.resolve_path(orig_raw_rel)
                            removed = self._safe_unlink(target)
                            if removed > 0:
                                freed_bytes += removed
                                deleted_files_count += 1
                                raw_used = max(0, raw_used - removed)
                            self._prune_empty_dirs(target.parent, raw_root)
                        except StorageError:
                            pass

                if (
                    pe_raw_rel is not None
                    and pe_raw_rel != orig_raw_rel
                    and sym.code_id is not None
                    and sym.code_file is not None
                    and pe_counts.get((sym.code_id, sym.code_file), 0) <= 0
                    and raw_counts.get(pe_raw_rel, 0) <= 0
                ):
                    try:
                        pe_target = self.resolve_path(pe_raw_rel)
                        removed_pe = self._safe_unlink(pe_target)
                        if removed_pe > 0:
                            freed_bytes += removed_pe
                            deleted_files_count += 1
                            raw_used = max(0, raw_used - removed_pe)
                        self._prune_empty_dirs(pe_target.parent, raw_root)
                    except StorageError:
                        pass

                sym.raw_archive_path = None
                sym.raw_size = 0

            # Sweep any remaining untracked raw files if still above target
            if raw_used > raw_target or needs_emergency_reclaim():
                for _, path, size in self._iter_files_sorted_by_age(raw_root):
                    if raw_used <= raw_target and not needs_emergency_reclaim():
                        break
                    removed = self._safe_unlink(path)
                    if removed > 0:
                        freed_bytes += removed
                        deleted_files_count += 1
                        raw_used = max(0, raw_used - size)
                        self._prune_empty_dirs(path.parent, raw_root)

        await session.flush()

        # Recompute global total usage
        total_quota_target = int(
            self.get_partition_quota_bytes("symbols/sym")
            + self.get_partition_quota_bytes("symbols/public_cache")
            + self.get_partition_quota_bytes("symbols/raw")
            + self.get_partition_quota_bytes("logs")
            + self.get_partition_quota_bytes("dumps")
            + self.get_partition_quota_bytes("staging")
        )
        db_used = await self.get_db_size_bytes(session)
        total_target = max(
            0,
            min(
                int(total_quota_target * threshold_ratio),
                int(self.settings.TOTAL_DISK_QUOTA_GB * GIB * threshold_ratio) - db_used,
            ),
        )

        def _fs_used() -> int:
            return sum(self.partition_used_bytes(p)[0] for p in PARTITIONS)

        # 3c. dumps LRU (unpinned only)
        dumps_used, _ = self.partition_used_bytes("dumps")
        dumps_target = int(self.get_partition_quota_bytes("dumps") * threshold_ratio)
        fs_used = _fs_used()
        if needs_emergency_reclaim() or dumps_used > dumps_target or fs_used > total_target:
            dumps_lru_stmt = (
                select(CrashReport, Issue)
                .outerjoin(Issue, CrashReport.issue_id == Issue.id)
                .where(CrashReport.dump_path.is_not(None))
                .order_by(CrashReport.created_at.asc())
            )
            for report, issue in (await session.execute(dumps_lru_stmt)).all():
                if (
                    dumps_used <= dumps_target
                    and fs_used <= total_target
                    and not needs_emergency_reclaim()
                ):
                    break
                if report.is_pinned or (issue is not None and issue.is_pinned):
                    skipped_pinned_dumps.add(report.id)
                    continue
                if report.status in ACTIVE_CRASH_STATUSES:
                    continue
                if report.dump_path:
                    try:
                        target = self.resolve_path(report.dump_path)
                        removed = self._safe_unlink(target)
                        if removed > 0:
                            freed_bytes += removed
                            deleted_files_count += 1
                            dumps_used = max(0, dumps_used - removed)
                            fs_used = max(0, fs_used - removed)
                        self._prune_empty_dirs(target.parent, dumps_root)
                    except StorageError:
                        pass
                    report.dump_path = None

        # 3d. logs LRU (unpinned only)
        logs_used, _ = self.partition_used_bytes("logs")
        logs_target = int(self.get_partition_quota_bytes("logs") * threshold_ratio)
        fs_used = _fs_used()
        if needs_emergency_reclaim() or logs_used > logs_target or fs_used > total_target:
            logs_lru_stmt = (
                select(CrashLog, CrashReport, Issue)
                .join(CrashReport, CrashLog.crash_id == CrashReport.id)
                .outerjoin(Issue, CrashReport.issue_id == Issue.id)
                .where(CrashLog.zst_path.is_not(None))
                .order_by(CrashReport.created_at.asc())
            )
            for log_row, report, issue in (await session.execute(logs_lru_stmt)).all():
                if (
                    logs_used <= logs_target
                    and fs_used <= total_target
                    and not needs_emergency_reclaim()
                ):
                    break
                if report.is_pinned or (issue is not None and issue.is_pinned):
                    skipped_pinned_logs.add(log_row.id)
                    continue
                if report.status in ACTIVE_CRASH_STATUSES:
                    continue
                for rel_attr in (log_row.zst_path, log_row.idx_path):
                    if rel_attr:
                        try:
                            target = self.resolve_path(rel_attr)
                            removed = self._safe_unlink(target)
                            if removed > 0:
                                freed_bytes += removed
                                deleted_files_count += 1
                                logs_used = max(0, logs_used - removed)
                                fs_used = max(0, fs_used - removed)
                            self._prune_empty_dirs(target.parent, logs_root)
                        except StorageError:
                            pass
                log_row.zst_path = None
                log_row.idx_path = None

        # 3e. symbols/sym LRU (only when symbols/sym itself or total quota is exceeded)
        sym_root = self.partition_path("symbols/sym")
        sym_used, _ = self.partition_used_bytes("symbols/sym")
        sym_target = int(self.get_partition_quota_bytes("symbols/sym") * threshold_ratio)
        fs_used = _fs_used()
        if sym_used > sym_target or fs_used > total_target:
            syms_lru_stmt = (
                select(Symbol)
                .where(Symbol.project_id.is_not(None))
                .order_by(Symbol.last_used_at.asc())
            )
            all_syms = (await session.execute(syms_lru_stmt)).scalars().all()
            path_counts: dict[str, int] = {}
            raw_counts: dict[str, int] = {}
            pe_counts: dict[tuple[str, str], int] = {}
            for s in all_syms:
                path_counts[s.sym_path] = path_counts.get(s.sym_path, 0) + 1
                if s.raw_archive_path:
                    raw_counts[s.raw_archive_path] = raw_counts.get(s.raw_archive_path, 0) + 1
                if s.code_id and s.code_file and s.has_pe_unwind:
                    pe_key = (s.code_id, s.code_file)
                    pe_counts[pe_key] = pe_counts.get(pe_key, 0) + 1

            for sym in all_syms:
                if sym_used <= sym_target and fs_used <= total_target:
                    break
                path_counts[sym.sym_path] = path_counts.get(sym.sym_path, 1) - 1
                if path_counts[sym.sym_path] <= 0:
                    try:
                        target = self.resolve_path(sym.sym_path)
                        removed = self._safe_unlink(target)
                        if removed > 0:
                            freed_bytes += removed
                            deleted_files_count += 1
                            sym_used = max(0, sym_used - removed)
                            fs_used = max(0, fs_used - removed)
                        self._prune_empty_dirs(target.parent, sym_root)
                    except StorageError:
                        pass

                pe_raw_rel: str | None = None
                if sym.code_id and sym.code_file and sym.has_pe_unwind:
                    pe_key = (sym.code_id, sym.code_file)
                    pe_counts[pe_key] = pe_counts.get(pe_key, 1) - 1
                    pe_raw_rel = f"symbols/raw/{sym.code_id}/{sym.code_file}.zst"

                if sym.raw_archive_path:
                    raw_rel = sym.raw_archive_path
                    raw_counts[raw_rel] = raw_counts.get(raw_rel, 1) - 1
                    pe_still_referenced = (
                        pe_raw_rel is not None
                        and raw_rel == pe_raw_rel
                        and sym.code_id is not None
                        and sym.code_file is not None
                        and pe_counts.get((sym.code_id, sym.code_file), 0) > 0
                    )
                    if raw_counts[raw_rel] <= 0 and not pe_still_referenced:
                        try:
                            raw_target_path = self.resolve_path(raw_rel)
                            removed_raw = self._safe_unlink(raw_target_path)
                            if removed_raw > 0:
                                freed_bytes += removed_raw
                                deleted_files_count += 1
                                fs_used = max(0, fs_used - removed_raw)
                            self._prune_empty_dirs(raw_target_path.parent, raw_root)
                        except StorageError:
                            pass

                if (
                    pe_raw_rel is not None
                    and pe_raw_rel != sym.raw_archive_path
                    and sym.code_id is not None
                    and sym.code_file is not None
                    and pe_counts.get((sym.code_id, sym.code_file), 0) <= 0
                    and raw_counts.get(pe_raw_rel, 0) <= 0
                ):
                    try:
                        pe_target_path = self.resolve_path(pe_raw_rel)
                        removed_pe = self._safe_unlink(pe_target_path)
                        if removed_pe > 0:
                            freed_bytes += removed_pe
                            deleted_files_count += 1
                            fs_used = max(0, fs_used - removed_pe)
                        self._prune_empty_dirs(pe_target_path.parent, raw_root)
                    except StorageError:
                        pass

                await session.delete(sym)

        await session.commit()

        db_used = await self.get_db_size_bytes(session)
        report_after = self.get_disk_usage_report(db_used_bytes=db_used)
        skipped_pinned_count = len(skipped_pinned_dumps) + len(skipped_pinned_logs)

        return {
            "freed_bytes": freed_bytes,
            "deleted_files_count": deleted_files_count,
            "skipped_pinned_count": skipped_pinned_count,
            "emergency_triggered": emergency,
            "partitions": report_after["partitions"],
        }


__all__ = ["GIB", "PARTITIONS", "StorageCapacityError", "StorageError", "StorageManager"]
