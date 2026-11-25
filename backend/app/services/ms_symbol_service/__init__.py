"""Non-blocking Microsoft Symbol Server (MSDL/SymSrv) fetcher and Breakpad .sym importer."""

from __future__ import annotations

from app.services.ms_symbol_service.cache import (
    _find_local_sym_in_root as _find_local_sym_in_root,
)
from app.services.ms_symbol_service.cache import (
    _has_local_sym_in_root as _has_local_sym_in_root,
)
from app.services.ms_symbol_service.cache import (
    _miss_cache_rel as _miss_cache_rel,
)
from app.services.ms_symbol_service.cache import (
    _public_cache_has_room as _public_cache_has_room,
)
from app.services.ms_symbol_service.cache import (
    _read_circuit_state as _read_circuit_state,
)
from app.services.ms_symbol_service.cache import (
    _record_circuit_network_failure as _record_circuit_network_failure,
)
from app.services.ms_symbol_service.cache import (
    _record_circuit_success as _record_circuit_success,
)
from app.services.ms_symbol_service.cache import (
    _sym_file_has_unwind as _sym_file_has_unwind,
)
from app.services.ms_symbol_service.cache import (
    _write_negative_cache as _write_negative_cache,
)
from app.services.ms_symbol_service.cache import (
    clear_negative_cache,
    get_negative_cache_info,
    has_cached_symbol,
    has_cached_symbol_detail,
    is_circuit_open,
    is_negative_cached,
    reset_circuit_breaker,
)
from app.services.ms_symbol_service.constants import (
    CAB_MAGIC as CAB_MAGIC,
)
from app.services.ms_symbol_service.constants import (
    CIRCUIT_COOLDOWN_SECONDS,
    CIRCUIT_FAILURE_THRESHOLD,
    SYMSRV_USER_AGENT,
)
from app.services.ms_symbol_service.constants import (
    CIRCUIT_STATE_REL as CIRCUIT_STATE_REL,
)
from app.services.ms_symbol_service.constants import (
    DOWNLOAD_CHUNK_BYTES as DOWNLOAD_CHUNK_BYTES,
)
from app.services.ms_symbol_service.constants import (
    KNOWN_WINDOWS_MODULE_PREFIXES as KNOWN_WINDOWS_MODULE_PREFIXES,
)
from app.services.ms_symbol_service.constants import (
    KNOWN_WINDOWS_SYSTEM_MODULES as KNOWN_WINDOWS_SYSTEM_MODULES,
)
from app.services.ms_symbol_service.constants import (
    MAX_MS_FETCH_CONCURRENCY as MAX_MS_FETCH_CONCURRENCY,
)
from app.services.ms_symbol_service.constants import (
    MAX_WINBINDEX_PE_PROBES as MAX_WINBINDEX_PE_PROBES,
)
from app.services.ms_symbol_service.constants import (
    MISS_CACHE_DIR_REL as MISS_CACHE_DIR_REL,
)
from app.services.ms_symbol_service.constants import (
    MSF7_MAGIC as MSF7_MAGIC,
)
from app.services.ms_symbol_service.constants import (
    PDB_MAGICS as PDB_MAGICS,
)
from app.services.ms_symbol_service.constants import (
    PE_MAGIC as PE_MAGIC,
)
from app.services.ms_symbol_service.constants import (
    PORTABLE_AGE as PORTABLE_AGE,
)
from app.services.ms_symbol_service.constants import (
    SYMSRV_SCHEME_PREFIX as SYMSRV_SCHEME_PREFIX,
)
from app.services.ms_symbol_service.constants import (
    TRANSIENT_MISS_TTL_SECONDS as TRANSIENT_MISS_TTL_SECONDS,
)
from app.services.ms_symbol_service.constants import (
    WINBINDEX_BASE_URL as WINBINDEX_BASE_URL,
)
from app.services.ms_symbol_service.downloader import (
    _build_symsrv_url as _build_symsrv_url,
)
from app.services.ms_symbol_service.downloader import (
    _can_extract_cab as _can_extract_cab,
)
from app.services.ms_symbol_service.downloader import (
    _check_file_magic as _check_file_magic,
)
from app.services.ms_symbol_service.downloader import (
    _compressed_symsrv_name as _compressed_symsrv_name,
)
from app.services.ms_symbol_service.downloader import (
    _download_matching_pe as _download_matching_pe,
)
from app.services.ms_symbol_service.downloader import (
    _download_symsrv_artifact as _download_symsrv_artifact,
)
from app.services.ms_symbol_service.downloader import (
    _extract_cab_file as _extract_cab_file,
)
from app.services.ms_symbol_service.downloader import (
    _stream_download_to_file as _stream_download_to_file,
)
from app.services.ms_symbol_service.identity import (
    _basename as _basename,
)
from app.services.ms_symbol_service.identity import (
    _is_microsoft_symsrv_url as _is_microsoft_symsrv_url,
)
from app.services.ms_symbol_service.identity import (
    _is_safe_segment as _is_safe_segment,
)
from app.services.ms_symbol_service.identity import (
    _is_symsrv_url as _is_symsrv_url,
)
from app.services.ms_symbol_service.identity import (
    _is_valid_pdb_identity as _is_valid_pdb_identity,
)
from app.services.ms_symbol_service.identity import (
    _is_valid_pe_identity as _is_valid_pe_identity,
)
from app.services.ms_symbol_service.identity import (
    _normalize_pe_code_ids as _normalize_pe_code_ids,
)
from app.services.ms_symbol_service.identity import (
    _parse_age_value as _parse_age_value,
)
from app.services.ms_symbol_service.identity import (
    _public_cache_sym_rel as _public_cache_sym_rel,
)
from app.services.ms_symbol_service.identity import (
    is_windows_system_module,
    normalize_debug_file_name,
    parse_manual_ms_target,
    split_public_symbol_urls,
)
from app.services.ms_symbol_service.importer import (
    _import_single_candidate as _import_single_candidate,
)
from app.services.ms_symbol_service.importer import (
    _invoke_callback as _invoke_callback,
)
from app.services.ms_symbol_service.importer import (
    _persist_public_msdl_symbol as _persist_public_msdl_symbol,
)
from app.services.ms_symbol_service.importer import (
    fetch_and_import_windows_symbols,
)
from app.services.ms_symbol_service.models import (
    MSFetchItemResult,
    MSFetchSummary,
    WindowsSymbolCandidate,
)
from app.services.ms_symbol_service.models import (
    _PdbMetadata as _PdbMetadata,
)
from app.services.ms_symbol_service.pdb_pe import (
    _fetch_winbindex_pe_keys as _fetch_winbindex_pe_keys,
)
from app.services.ms_symbol_service.pdb_pe import (
    _pe_matches_pdb as _pe_matches_pdb,
)
from app.services.ms_symbol_service.pdb_pe import (
    _read_native_pdb_metadata as _read_native_pdb_metadata,
)
from app.services.ms_symbol_service.pdb_pe import (
    _read_pdb_metadata as _read_pdb_metadata,
)
from app.services.ms_symbol_service.selection import select_windows_system_candidates

__all__ = [
    "CIRCUIT_COOLDOWN_SECONDS",
    "CIRCUIT_FAILURE_THRESHOLD",
    "MSFetchItemResult",
    "MSFetchSummary",
    "SYMSRV_USER_AGENT",
    "WindowsSymbolCandidate",
    "clear_negative_cache",
    "fetch_and_import_windows_symbols",
    "get_negative_cache_info",
    "has_cached_symbol",
    "has_cached_symbol_detail",
    "is_circuit_open",
    "is_negative_cached",
    "is_windows_system_module",
    "normalize_debug_file_name",
    "parse_manual_ms_target",
    "reset_circuit_breaker",
    "select_windows_system_candidates",
    "split_public_symbol_urls",
]
