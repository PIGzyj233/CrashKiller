"""Core types, errors, and size limits for Breakpad symbol ingestion."""

from __future__ import annotations

from dataclasses import dataclass

from app.db.models import Symbol

MAX_SYMBOL_OUTPUT_BYTES = 128 * 1024**2
MAX_SYMBOL_TOOL_MEMORY_BYTES = 1536 * 1024**2


class SymbolConversionError(Exception):
    """Raised when dump_syms fails or produces invalid Breakpad .sym output."""


@dataclass(slots=True)
class ParsedSymHeader:
    os_name: str
    arch: str
    debug_id: str
    debug_file: str
    code_id: str | None
    code_file: str | None
    module_name: str
    stem: str
    has_unwind_records: bool


@dataclass(slots=True)
class SymbolIngestResult:
    symbol: Symbol
    deduplicated: bool
    upgraded: bool
