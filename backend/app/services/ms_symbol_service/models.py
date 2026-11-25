"""Data models for Microsoft Symbol Server (MSDL) import operations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.services.ms_symbol_service.constants import PORTABLE_AGE


@dataclass(frozen=True, slots=True)
class WindowsSymbolCandidate:
    """A missing Windows system module eligible for MSDL symbol import."""

    module_name: str
    debug_file: str
    debug_id: str
    code_file: str | None = None
    code_id: str | None = None
    filename: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "module_name": self.module_name,
            "debug_file": self.debug_file,
            "debug_id": self.debug_id,
            "code_file": self.code_file,
            "code_id": self.code_id,
            "filename": self.filename,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WindowsSymbolCandidate | None:
        debug_file = str(data.get("debug_file") or "").strip()
        debug_id = str(data.get("debug_id") or "").strip().upper()
        if not debug_file or not debug_id:
            return None
        module_name = str(data.get("module_name") or debug_file).strip()
        code_file_raw = data.get("code_file")
        code_id_raw = data.get("code_id")
        filename_raw = data.get("filename")
        return cls(
            module_name=module_name,
            debug_file=debug_file,
            debug_id=debug_id,
            code_file=str(code_file_raw).strip() if code_file_raw else None,
            code_id=str(code_id_raw).strip().upper() if code_id_raw else None,
            filename=str(filename_raw).strip() if filename_raw else None,
        )


@dataclass(frozen=True, slots=True)
class MSFetchItemResult:
    """Per-target outcome for an MSDL symbol import attempt."""

    module_name: str
    debug_file: str
    debug_id: str
    status: str
    detail: str | None = None
    has_pe_unwind: bool = False
    symbol_id: uuid.UUID | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_name": self.module_name,
            "debug_file": self.debug_file,
            "debug_id": self.debug_id,
            "status": self.status,
            "detail": self.detail,
            "has_pe_unwind": self.has_pe_unwind,
            "symbol_id": str(self.symbol_id) if self.symbol_id is not None else None,
        }


@dataclass(slots=True)
class MSFetchSummary:
    """Outcome counters for a batch of MSDL symbol import attempts."""

    attempted: int = 0
    imported: int = 0
    upgraded: int = 0
    cached_hit: int = 0
    not_found: int = 0
    skipped: int = 0
    failed: int = 0
    circuit_open: bool = False
    imported_keys: list[str] = field(default_factory=list)
    items: list[MSFetchItemResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "imported": self.imported,
            "upgraded": self.upgraded,
            "cached_hit": self.cached_hit,
            "not_found": self.not_found,
            "skipped": self.skipped,
            "failed": self.failed,
            "circuit_open": self.circuit_open,
            "imported_keys": list(self.imported_keys),
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class _PdbMetadata:
    guid: uuid.UUID
    age: int
    signature: int
    portable: bool = False
    machine: int = 0
    image_size: int = 0

    @property
    def key(self) -> str:
        age = PORTABLE_AGE if self.portable else self.age
        return f"{self.guid.hex.upper()}{age:X}"
