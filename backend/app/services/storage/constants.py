"""Shared storage constants and exception types."""

from __future__ import annotations

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
