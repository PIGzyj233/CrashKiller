from __future__ import annotations

import collections
from typing import Any


class FakeDiskUsage:
    """Controllable shutil.disk_usage proxy for HOST_SAFETY_FLOOR_GB tests."""

    _Usage = collections.namedtuple("usage", ["total", "used", "free"])

    def __init__(self, free_gb: float = 200.0, total_gb: float = 500.0) -> None:
        self._total_bytes = int(total_gb * (1024**3))
        self._free_bytes = int(free_gb * (1024**3))

    def set_free_gb(self, free_gb: float, total_gb: float = 500.0) -> None:
        self._total_bytes = int(total_gb * (1024**3))
        self._free_bytes = int(free_gb * (1024**3))

    def get_free_gb(self) -> float:
        return self._free_bytes / (1024**3)

    def __call__(self, path: Any) -> Any:
        used = max(0, self._total_bytes - self._free_bytes)
        return self._Usage(self._total_bytes, used, self._free_bytes)


DISK_USAGE_PROXY = FakeDiskUsage(free_gb=200.0, total_gb=500.0)
_DISK_USAGE_PROXY = DISK_USAGE_PROXY
