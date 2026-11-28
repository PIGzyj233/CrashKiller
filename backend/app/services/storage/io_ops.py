"""POSIX locking, staging workspaces, path resolution, and atomic/zstd file I/O."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

import zstandard as zstd

from app.services.storage.constants import PARTITIONS, StorageError

if TYPE_CHECKING:
    from app.config import Settings


class StorageIOMixin:
    """Filesystem layout, locking, safe path resolution, and atomic zstd I/O."""

    data_dir: Path
    settings: Settings

    if TYPE_CHECKING:

        def is_host_floor_breached(self) -> bool: ...

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
        async with self.rw_lock(purpose, exclusive=True):
            yield

    @asynccontextmanager
    async def rw_lock(self, purpose: str, *, exclusive: bool = False) -> AsyncIterator[None]:
        """Cross-process POSIX reader-writer flock (LOCK_SH for readers, LOCK_EX for writers)."""
        mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        with (self.data_dir / f".{purpose}.lock").open("ab") as lock:
            while True:
                try:
                    fcntl.flock(lock, mode)
                except BlockingIOError:
                    await asyncio.sleep(0.01)
                else:
                    break
            yield

    @asynccontextmanager
    async def storage_io_lock(self, *, exclusive: bool = False) -> AsyncIterator[None]:
        """Coordinate concurrent file+ledger writers (LOCK_SH) with reconciliation (LOCK_EX)."""
        async with self.rw_lock("storage-io", exclusive=exclusive):
            yield

    @asynccontextmanager
    async def project_sym_lock(self, *, exclusive: bool = False) -> AsyncIterator[None]:
        """Coordinate project symbol publication (LOCK_SH) with project-symbol GC (LOCK_EX)."""
        async with self.rw_lock("project-sym", exclusive=exclusive):
            yield

    @asynccontextmanager
    async def public_cache_lock(self, *, exclusive: bool = False) -> AsyncIterator[None]:
        """Coordinate public symbol publication (LOCK_SH) with public-cache GC (LOCK_EX)."""
        async with self.rw_lock("public-cache", exclusive=exclusive):
            yield

    @asynccontextmanager
    async def raw_symbol_lock(self, *, exclusive: bool = False) -> AsyncIterator[None]:
        """Coordinate raw symbol archive publication (LOCK_SH) with raw-symbol GC (LOCK_EX)."""
        async with self.rw_lock("raw-symbols", exclusive=exclusive):
            yield

    @contextmanager
    def staging_upload_lease(self, kind: str, key: str) -> Iterator[None]:
        """Hold a POSIX lease protecting an in-flight upload directory from GC before DB commit."""
        root = self.partition_path("staging")
        marker = root / f".tmp_{kind}_{key}.lock"
        with marker.open("ab") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX)
            try:
                yield
            finally:
                marker.unlink(missing_ok=True)

    @asynccontextmanager
    async def async_staging_upload_lease(self, kind: str, key: str) -> AsyncIterator[None]:
        """Non-blocking async POSIX lease protecting an in-flight upload directory from GC."""
        root = self.partition_path("staging")
        marker = root / f".tmp_{kind}_{key}.lock"
        with marker.open("ab") as lease:
            while True:
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    await asyncio.sleep(0.01)
                else:
                    break
            try:
                yield
            finally:
                marker.unlink(missing_ok=True)

    @staticmethod
    def _staging_upload_lease_active(root: Path, kind: str, key: str) -> bool:
        try:
            with (root / f".tmp_{kind}_{key}.lock").open("rb") as lease:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except FileNotFoundError:
            return False
        except BlockingIOError:
            return True
        return False

    def write_staging_queue_lease(
        self,
        rel_dir: str,
        ttl_seconds: float,
        *,
        job_id: str | None = None,
        queue_name: str | None = None,
        mode: str | None = None,
    ) -> Path:
        """Write a .tmp_lease.json marker protecting a queued staging directory."""
        dest = self.resolve_path(f"{rel_dir}/.tmp_lease.json")
        dest.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "expires_at": time.time() + max(1.0, float(ttl_seconds)),
            "created_at": time.time(),
            "pid": os.getpid(),
        }
        if job_id:
            payload["job_id"] = job_id
        if queue_name:
            payload["queue_name"] = queue_name
        if mode:
            payload["mode"] = mode
        dest.write_text(json.dumps(payload), encoding="utf-8")
        return dest

    @staticmethod
    def _read_staging_queue_lease(dir_path: Path) -> dict[str, Any] | None:
        lease_file = dir_path / ".tmp_lease.json"
        if not lease_file.is_file():
            return None
        try:
            data = json.loads(lease_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def _staging_queue_lease_active(dir_path: Path) -> bool:
        data = StorageIOMixin._read_staging_queue_lease(dir_path)
        if data is None:
            return False
        try:
            return time.time() < float(data.get("expires_at", 0.0))
        except (ValueError, TypeError):
            return False

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
        *,
        cancel_event: threading.Event | None = None,
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
                    if cancel_event is not None and cancel_event.is_set():
                        raise StorageError("Dump compression cancelled")
                    buf = src_fh.read(65536)
                    if not buf:
                        break
                    raw_size += len(buf)
                    zw.write(buf)
            dst_fh.flush()
            os.fsync(dst_fh.fileno())

        try:
            if cancel_event is not None and cancel_event.is_set():
                raise StorageError("Dump compression cancelled")
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
            if cancel_event is not None and cancel_event.is_set():
                raise StorageError("Dump compression cancelled")
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
