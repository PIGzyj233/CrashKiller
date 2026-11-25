"""Breakpad symbol conversion (dump_syms), deduplication, raw archiving, and SymSrv."""

from __future__ import annotations

from app.services.symbol_service.archiving import (
    _archive_raw_files as _archive_raw_files,
)
from app.services.symbol_service.archiving import (
    _build_raw_archive_paths as _build_raw_archive_paths,
)
from app.services.symbol_service.archiving import (
    _input_size as _input_size,
)
from app.services.symbol_service.archiving import (
    _stage_input as _stage_input,
)
from app.services.symbol_service.conversion import (
    _pe_debug_identity as _pe_debug_identity,
)
from app.services.symbol_service.conversion import (
    _run_subprocess as _run_subprocess,
)
from app.services.symbol_service.conversion import (
    run_dump_syms,
)
from app.services.symbol_service.ingestion import (
    _clear_msdl_negative_cache as _clear_msdl_negative_cache,
)
from app.services.symbol_service.ingestion import (
    _scan_unindexed_public_syms as _scan_unindexed_public_syms,
)
from app.services.symbol_service.ingestion import (
    _store_symbol as _store_symbol,
)
from app.services.symbol_service.ingestion import (
    _unlink_public_sym_if_distinct as _unlink_public_sym_if_distinct,
)
from app.services.symbol_service.ingestion import (
    ingest_public_symbol,
    ingest_symbol,
    reconcile_public_symbol_cache,
    store_public_symbol,
)
from app.services.symbol_service.models import (
    MAX_SYMBOL_OUTPUT_BYTES as MAX_SYMBOL_OUTPUT_BYTES,
)
from app.services.symbol_service.models import (
    MAX_SYMBOL_TOOL_MEMORY_BYTES as MAX_SYMBOL_TOOL_MEMORY_BYTES,
)
from app.services.symbol_service.models import (
    ParsedSymHeader,
    SymbolConversionError,
    SymbolIngestResult,
)
from app.services.symbol_service.parsing import (
    _module_identity as _module_identity,
)
from app.services.symbol_service.parsing import (
    _parse_sym_records,
    parse_sym_header,
)
from app.services.symbol_service.parsing import (
    _resolve_in_partition as _resolve_in_partition,
)
from app.services.symbol_service.parsing import (
    _sanitize_filename as _sanitize_filename,
)
from app.services.symbol_service.parsing import (
    _validate_path_segment as _validate_path_segment,
)
from app.services.symbol_service.parsing import (
    clamp_upload_filename as clamp_upload_filename,
)
from app.services.symbol_service.symsrv import (
    resolve_symsrv_file,
    stream_decompressed_zst,
)

__all__ = [
    "ParsedSymHeader",
    "SymbolConversionError",
    "SymbolIngestResult",
    "_parse_sym_records",
    "ingest_public_symbol",
    "ingest_symbol",
    "parse_sym_header",
    "reconcile_public_symbol_cache",
    "resolve_symsrv_file",
    "run_dump_syms",
    "store_public_symbol",
    "stream_decompressed_zst",
]
