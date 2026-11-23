"""Non-blocking Microsoft Symbol Server (MSDL/SymSrv) fetcher and Breakpad .sym importer."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.config import Settings
from app.services.settings_service import DEFAULT_NOISE_MODULES
from app.services.storage import StorageError, StorageManager
from app.services.symbol_service import (
    SymbolConversionError,
    _resolve_in_partition,
    _validate_path_segment,
    parse_sym_header,
    run_dump_syms,
)

logger = logging.getLogger(__name__)

SYMSRV_USER_AGENT = "Microsoft-Symbol-Server/10.0.10522.521"
SYMSRV_SCHEME_PREFIX = "symsrv+"

CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 600
TRANSIENT_MISS_TTL_SECONDS = 3600
MAX_MS_FETCH_CONCURRENCY = 2
DOWNLOAD_CHUNK_BYTES = 65536

MISS_CACHE_DIR_REL = "symbols/public_cache/_msdl_miss"
CIRCUIT_STATE_REL = "symbols/public_cache/_msdl_circuit.json"

PDB_MAGICS: tuple[bytes, ...] = (
    b"Microsoft C/C++ MSF 7.00\r\n\x1aDS",
    b"Microsoft C/C++ program database 2.00\r\n\x1aJG",
)
PE_MAGIC = b"MZ"
CAB_MAGIC = b"MSCF"

_DEBUG_ID_RE = re.compile(r"^[0-9A-Fa-f]{33,40}$")
_CODE_ID_RE = re.compile(r"^[0-9A-Fa-f]{8,24}$")
_ALL_ZERO_RE = re.compile(r"^0+$")

_WINDOWS_SYSTEM_PATH_RE = re.compile(
    r"^[a-z]:/windows/"
    r"(?:system32|syswow64|winsxs|systemapps|microsoft\.net|"
    r"servicing|immersivecontrolpanel|systemresources|assembly)/",
    re.IGNORECASE,
)

KNOWN_WINDOWS_SYSTEM_MODULES: frozenset[str] = frozenset(
    {
        *(m.lower() for m in DEFAULT_NOISE_MODULES),
        "gdi32full.dll",
        "combase.dll",
        "oleaut32.dll",
        "shell32.dll",
        "shlwapi.dll",
        "shcore.dll",
        "ws2_32.dll",
        "winhttp.dll",
        "wininet.dll",
        "crypt32.dll",
        "bcrypt.dll",
        "bcryptprimitives.dll",
        "dbghelp.dll",
        "dbgcore.dll",
        "imm32.dll",
        "uxtheme.dll",
        "dwmapi.dll",
        "msctf.dll",
        "powrprof.dll",
        "setupapi.dll",
        "cfgmgr32.dll",
        "winmm.dll",
        "avrt.dll",
        "mmdevapi.dll",
        "audioses.dll",
        "mswsock.dll",
        "dnsapi.dll",
        "iphlpapi.dll",
        "netapi32.dll",
        "sspicli.dll",
        "cryptbase.dll",
        "ntmarta.dll",
        "version.dll",
        "psapi.dll",
        "wtsapi32.dll",
        "userenv.dll",
        "profapi.dll",
        "propsys.dll",
        "wintypes.dll",
        "d3d9.dll",
        "d3d10.dll",
        "d3d10_1.dll",
        "d3d11.dll",
        "d3d12.dll",
        "d3d12core.dll",
        "d3dcompiler_47.dll",
        "dxgi.dll",
        "dxcore.dll",
        "d2d1.dll",
        "dwrite.dll",
        "dxil.dll",
        "dxcompiler.dll",
        "xinput1_4.dll",
        "xinput1_3.dll",
        "xaudio2_9.dll",
        "clr.dll",
        "coreclr.dll",
        "mscoree.dll",
        "mscorwks.dll",
        "msvcp140_2.dll",
        "msvcp140_atomic_wait.dll",
        "msvcp140_codecvt_ids.dll",
        "vcruntime140_threads.dll",
        "msvcr120.dll",
        "msvcp120.dll",
        "msvcr110.dll",
        "msvcp110.dll",
        "msvcr100.dll",
        "msvcp100.dll",
        "vccorlib140.dll",
        "mfc140u.dll",
        "mfc140.dll",
    }
)

KNOWN_WINDOWS_MODULE_PREFIXES: tuple[str, ...] = (
    "api-ms-win-",
    "ext-ms-win-",
    "windows.",
    "onecore",
    "vcruntime140",
    "msvcp140",
    "ucrtbase",
    "concrt140",
    "vcomp140",
    "d3dcompiler_",
    "xaudio2_",
    "xinput1_",
    "wow64",
)


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


@dataclass(slots=True)
class MSFetchSummary:
    """Outcome counters for a batch of MSDL symbol import attempts."""

    attempted: int = 0
    imported: int = 0
    cached_hit: int = 0
    not_found: int = 0
    skipped: int = 0
    failed: int = 0
    circuit_open: bool = False
    imported_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "imported": self.imported,
            "cached_hit": self.cached_hit,
            "not_found": self.not_found,
            "skipped": self.skipped,
            "failed": self.failed,
            "circuit_open": self.circuit_open,
            "imported_keys": list(self.imported_keys),
        }


def _basename(path_str: str) -> str:
    cleaned = path_str.replace("\\", "/").rstrip("/")
    return cleaned.rsplit("/", 1)[-1] if cleaned else ""


def _is_safe_segment(value: str) -> bool:
    try:
        _validate_path_segment(value, "segment")
    except SymbolConversionError:
        return False
    return True


def _is_symsrv_url(raw_url: str) -> tuple[bool, str]:
    """Return (is_symsrv, normalized_http_url) for a configured symbol URL."""
    url = raw_url.strip()
    if not url:
        return False, ""
    if url.lower().startswith(SYMSRV_SCHEME_PREFIX):
        stripped = url[len(SYMSRV_SCHEME_PREFIX) :].strip()
        return True, stripped

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower().rstrip("/")
    if host == "msdl.microsoft.com":
        return True, url
    if host.endswith(".microsoft.com") and "/symbols" in path:
        return True, url
    if path.endswith("/download/symbols") or path.endswith("/symsrv"):
        return True, url
    return False, url


def split_public_symbol_urls(urls: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split PUBLIC_SYMBOL_URLS into (breakpad_sym_urls, symsrv_pdb_urls)."""
    breakpad_urls: list[str] = []
    symsrv_urls: list[str] = []
    seen_bp: set[str] = set()
    seen_ss: set[str] = set()

    for raw in urls:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip()
        if not cleaned:
            continue
        is_ss, normalized = _is_symsrv_url(cleaned)
        if not normalized:
            continue
        parsed = urlparse(normalized)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            continue
        if is_ss:
            key = normalized.rstrip("/").lower()
            if key not in seen_ss:
                seen_ss.add(key)
                symsrv_urls.append(normalized)
        else:
            key = normalized.rstrip("/").lower()
            if key not in seen_bp:
                seen_bp.add(key)
                breakpad_urls.append(normalized)

    return breakpad_urls, symsrv_urls


def is_windows_system_module(
    *,
    filename: str | None,
    code_file: str | None,
    module_name: str | None,
    debug_file: str | None,
) -> bool:
    """Return True only when a module clearly belongs to Windows OS or Microsoft runtimes."""
    if not debug_file or not debug_file.strip().lower().endswith(".pdb"):
        return False

    for raw_path in (filename, code_file):
        if not raw_path:
            continue
        norm = raw_path.strip().replace("\\", "/")
        norm_low = norm.lower()
        if "/driverstore/filerepository/" in norm_low:
            continue
        if _WINDOWS_SYSTEM_PATH_RE.match(norm):
            return True

    names: list[str] = []
    for candidate_name in (module_name, _basename(filename or ""), _basename(code_file or "")):
        if candidate_name:
            low = candidate_name.strip().lower()
            if low:
                names.append(low)

    for name in names:
        if name in KNOWN_WINDOWS_SYSTEM_MODULES:
            return True
        if any(name.startswith(prefix) for prefix in KNOWN_WINDOWS_MODULE_PREFIXES):
            return True

    return False


def _is_valid_pdb_identity(debug_file: str, debug_id: str) -> bool:
    if not debug_file or not debug_file.lower().endswith(".pdb"):
        return False
    if len(debug_file) > 255 or not _is_safe_segment(debug_file):
        return False
    if not debug_id or not _DEBUG_ID_RE.match(debug_id):
        return False
    if _ALL_ZERO_RE.match(debug_id):
        return False
    return True


def _is_valid_pe_identity(code_file: str | None, code_id: str | None) -> tuple[str, str] | None:
    if not code_file or not code_id:
        return None
    base = _basename(code_file).strip()
    cid = code_id.strip().upper()
    if not base or len(base) > 255 or not _is_safe_segment(base):
        return None
    if not (base.lower().endswith(".dll") or base.lower().endswith(".exe")):
        return None
    if not _CODE_ID_RE.match(cid) or _ALL_ZERO_RE.match(cid):
        return None
    return base, cid


def _public_cache_sym_rel(debug_file: str, debug_id: str) -> str:
    safe_df = _validate_path_segment(debug_file, "debug_file")
    safe_did = _validate_path_segment(debug_id.upper(), "debug_id")
    stem = _validate_path_segment(Path(safe_df).stem or safe_df, "stem")
    return f"symbols/public_cache/{safe_df}/{safe_did}/{stem}.sym"


def _has_local_sym_in_root(root: Path, debug_file: str, debug_id: str) -> bool:
    if not root.exists():
        return False
    stem = Path(debug_file).stem or debug_file
    exact = root / debug_file / debug_id.upper() / f"{stem}.sym"
    if exact.is_file():
        return True
    # Case-insensitive fallback for Linux hosts storing Windows PDB names
    df_low = debug_file.lower()
    did_low = debug_id.lower()
    sym_low = f"{stem}.sym".lower()
    try:
        for df_entry in root.iterdir():
            if not df_entry.is_dir() or df_entry.name.lower() != df_low:
                continue
            for id_entry in df_entry.iterdir():
                if not id_entry.is_dir() or id_entry.name.lower() != did_low:
                    continue
                for file_entry in id_entry.iterdir():
                    if file_entry.is_file() and file_entry.name.lower() == sym_low:
                        return True
    except OSError:
        return False
    return False


def has_cached_symbol(
    storage: StorageManager,
    debug_file: str,
    debug_id: str,
    project_id: uuid.UUID | str | None = None,
) -> bool:
    """Return True if a Breakpad .sym already exists in project symbols or public_cache."""
    if project_id is not None:
        proj_seg = str(project_id).strip()
        if _is_safe_segment(proj_seg):
            try:
                proj_root = storage.resolve_path(f"symbols/sym/{proj_seg}")
            except StorageError:
                proj_root = None
            if proj_root is not None and _has_local_sym_in_root(proj_root, debug_file, debug_id):
                return True
    pub_root = storage.partition_path("symbols/public_cache")
    return _has_local_sym_in_root(pub_root, debug_file, debug_id)


def _miss_cache_rel(debug_file: str, debug_id: str) -> str:
    digest = hashlib.sha256(f"{debug_file.lower()}:{debug_id.upper()}".encode()).hexdigest()[:32]
    return f"{MISS_CACHE_DIR_REL}/{digest}.json"


def is_negative_cached(storage: StorageManager, debug_file: str, debug_id: str) -> bool:
    """Return True when (debug_file, debug_id) has an unexpired negative-cache entry."""
    rel = _miss_cache_rel(debug_file, debug_id)
    try:
        path = storage.resolve_path(rel)
    except StorageError:
        return False
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        expires_at = float(payload.get("expires_at", 0.0))
    except (OSError, ValueError, TypeError):
        return False
    if time.time() < expires_at:
        return True
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return False


def _write_negative_cache(
    storage: StorageManager,
    debug_file: str,
    debug_id: str,
    reason: str,
    ttl_seconds: int,
) -> None:
    if ttl_seconds <= 0 or storage.is_host_floor_breached():
        return
    rel = _miss_cache_rel(debug_file, debug_id)
    payload = {
        "debug_file": debug_file,
        "debug_id": debug_id.upper(),
        "reason": reason,
        "expires_at": time.time() + ttl_seconds,
    }
    try:
        storage.atomic_write_bytes(rel, json.dumps(payload).encode("utf-8"))
    except (OSError, StorageError):
        logger.debug("Could not persist MSDL negative cache for %s/%s", debug_file, debug_id)


def _read_circuit_state(storage: StorageManager) -> dict[str, Any]:
    try:
        path = storage.resolve_path(CIRCUIT_STATE_REL)
    except StorageError:
        return {"consecutive_failures": 0, "open_until": 0.0}
    if not path.is_file():
        return {"consecutive_failures": 0, "open_until": 0.0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"consecutive_failures": 0, "open_until": 0.0}


def is_circuit_open(storage: StorageManager) -> bool:
    """Return True when MSDL outbound fetches are temporarily paused due to network errors."""
    state = _read_circuit_state(storage)
    try:
        return time.time() < float(state.get("open_until", 0.0))
    except (TypeError, ValueError):
        return False


def _record_circuit_success(storage: StorageManager) -> None:
    state = _read_circuit_state(storage)
    if not state.get("consecutive_failures") and not state.get("open_until"):
        return
    try:
        storage.atomic_write_bytes(
            CIRCUIT_STATE_REL,
            json.dumps({"consecutive_failures": 0, "open_until": 0.0}).encode("utf-8"),
        )
    except (OSError, StorageError):
        pass


def _record_circuit_network_failure(storage: StorageManager, error: str) -> bool:
    """Record a connectivity failure and return True if the circuit is now open."""
    state = _read_circuit_state(storage)
    failures = int(state.get("consecutive_failures") or 0) + 1
    open_until = 0.0
    tripped = failures >= CIRCUIT_FAILURE_THRESHOLD
    if tripped:
        open_until = time.time() + CIRCUIT_COOLDOWN_SECONDS
    payload = {
        "consecutive_failures": failures,
        "open_until": open_until,
        "last_error": error[:200],
    }
    try:
        if not storage.is_host_floor_breached():
            storage.atomic_write_bytes(CIRCUIT_STATE_REL, json.dumps(payload).encode("utf-8"))
    except (OSError, StorageError):
        pass
    return tripped


def select_windows_system_candidates(
    *,
    os_version: str | None,
    parsed_stacks_json: dict[str, Any] | None,
    missing_symbols_json: list[dict[str, Any]] | None,
    storage: StorageManager,
    settings: Settings,
    project_id: uuid.UUID | str | None = None,
) -> list[WindowsSymbolCandidate]:
    """Select stack-referenced missing Windows system modules eligible for MSDL import."""
    if os_version and "windows" not in os_version.lower():
        return []
    if not parsed_stacks_json or not isinstance(parsed_stacks_json, dict):
        return []

    max_modules = max(0, int(settings.MS_SYMBOL_MAX_MODULES_PER_CRASH))
    if max_modules == 0:
        return []

    modules = parsed_stacks_json.get("modules") or []
    missing_by_mod: dict[str, dict[str, Any]] = {}

    for mod in modules:
        if not isinstance(mod, dict):
            continue
        loaded = bool(mod.get("loaded_symbols", False))
        is_missing = mod.get("missing_symbols")
        if loaded and is_missing is not True:
            continue
        debug_file = str(mod.get("debug_file") or "").strip()
        debug_id = str(mod.get("debug_id") or "").strip().upper()
        if not _is_valid_pdb_identity(debug_file, debug_id):
            continue
        mod_name = str(mod.get("module_name") or _basename(str(mod.get("filename") or ""))).strip()
        if not mod_name:
            continue
        missing_by_mod.setdefault(
            mod_name.lower(),
            {
                "module_name": mod_name,
                "filename": mod.get("filename"),
                "debug_file": debug_file,
                "debug_id": debug_id,
                "code_file": mod.get("code_file") or mod.get("filename"),
                "code_id": mod.get("code_id"),
            },
        )

    for entry in missing_symbols_json or []:
        if not isinstance(entry, dict):
            continue
        debug_file = str(entry.get("debug_file") or "").strip()
        debug_id = str(entry.get("debug_id") or "").strip().upper()
        if not _is_valid_pdb_identity(debug_file, debug_id):
            continue
        mod_name = str(entry.get("module_name") or "").strip()
        if not mod_name:
            continue
        missing_by_mod.setdefault(
            mod_name.lower(),
            {
                "module_name": mod_name,
                "filename": entry.get("code_file"),
                "debug_file": debug_file,
                "debug_id": debug_id,
                "code_file": entry.get("code_file"),
                "code_id": entry.get("code_id"),
            },
        )

    if not missing_by_mod:
        return []

    threads = parsed_stacks_json.get("threads") or []
    crashing_idx = parsed_stacks_json.get("crashing_thread_index", 0)
    ordered_threads: list[dict[str, Any]] = []
    if isinstance(crashing_idx, int) and 0 <= crashing_idx < len(threads):
        ct = threads[crashing_idx]
        if isinstance(ct, dict):
            ordered_threads.append(ct)
    for idx, thread in enumerate(threads):
        if idx == crashing_idx or not isinstance(thread, dict):
            continue
        ordered_threads.append(thread)

    candidates: list[WindowsSymbolCandidate] = []
    seen_keys: set[tuple[str, str]] = set()

    for thread in ordered_threads:
        for frame in thread.get("frames") or []:
            if not isinstance(frame, dict):
                continue
            raw_mod = frame.get("module")
            if not isinstance(raw_mod, str) or not raw_mod.strip():
                continue
            mod_key = _basename(raw_mod.strip()).lower()
            info = missing_by_mod.get(mod_key)
            if info is None:
                continue

            debug_file = str(info["debug_file"])
            debug_id = str(info["debug_id"]).upper()
            dedup_key = (debug_file.lower(), debug_id)
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            filename_val = str(info["filename"]).strip() if info.get("filename") else None
            code_file_val = str(info["code_file"]).strip() if info.get("code_file") else None
            code_id_val = str(info["code_id"]).strip().upper() if info.get("code_id") else None

            if not is_windows_system_module(
                filename=filename_val,
                code_file=code_file_val,
                module_name=str(info["module_name"]),
                debug_file=debug_file,
            ):
                continue

            if has_cached_symbol(storage, debug_file, debug_id, project_id=project_id):
                continue
            if is_negative_cached(storage, debug_file, debug_id):
                continue

            candidates.append(
                WindowsSymbolCandidate(
                    module_name=str(info["module_name"]),
                    debug_file=debug_file,
                    debug_id=debug_id,
                    code_file=code_file_val,
                    code_id=code_id_val,
                    filename=filename_val,
                )
            )
            if len(candidates) >= max_modules:
                return candidates

    return candidates


def _compressed_symsrv_name(filename: str) -> str | None:
    if len(filename) < 2 or filename.endswith("_"):
        return None
    return f"{filename[:-1]}_"


def _build_symsrv_url(base_url: str, filename: str, symbol_id: str, leaf_name: str) -> str:
    return (
        f"{base_url.rstrip('/')}/"
        f"{quote(filename, safe='')}/"
        f"{quote(symbol_id, safe='')}/"
        f"{quote(leaf_name, safe='')}"
    )


def _check_file_magic(path: Path, expected_magics: tuple[bytes, ...]) -> bool:
    try:
        size = path.stat().st_size
        if size < 64:
            return False
        max_len = max(len(m) for m in expected_magics)
        with path.open("rb") as fh:
            head = fh.read(max_len)
        return any(head.startswith(m) for m in expected_magics)
    except OSError:
        return False


async def _stream_download_to_file(
    client: httpx.AsyncClient,
    url: str,
    dest_path: Path,
    max_bytes: int,
    timeout_seconds: float,
) -> tuple[str, str]:
    """Stream an HTTP GET response to dest_path with a hard byte cap.

    Returns (status_code_label, detail) where label is one of:
    'ok', 'not_found', 'too_large', 'network_error'.
    """
    req_timeout = httpx.Timeout(
        connect=min(5.0, timeout_seconds),
        read=min(20.0, timeout_seconds),
        write=5.0,
        pool=5.0,
    )
    try:
        async with asyncio.timeout(timeout_seconds):
            async with client.stream(
                "GET",
                url,
                headers={"User-Agent": SYMSRV_USER_AGENT},
                timeout=req_timeout,
            ) as response:
                if response.status_code in {404, 410}:
                    return "not_found", f"HTTP {response.status_code}"
                if response.status_code == 429 or response.status_code >= 500:
                    return "network_error", f"HTTP {response.status_code}"
                if response.status_code != 200:
                    return "not_found", f"HTTP {response.status_code}"

                raw_len = response.headers.get("content-length")
                if raw_len is not None:
                    try:
                        if int(raw_len) > max_bytes:
                            return "too_large", f"Content-Length {raw_len} exceeds {max_bytes}"
                    except ValueError:
                        pass

                written = 0
                with dest_path.open("wb") as fh:
                    async for chunk in response.aiter_bytes(DOWNLOAD_CHUNK_BYTES):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > max_bytes:
                            fh.close()
                            dest_path.unlink(missing_ok=True)
                            return "too_large", f"Stream exceeded {max_bytes} bytes"
                        fh.write(chunk)
                return "ok", ""
    except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as exc:
        dest_path.unlink(missing_ok=True)
        return "network_error", str(exc) or type(exc).__name__
    except OSError as exc:
        dest_path.unlink(missing_ok=True)
        return "network_error", str(exc) or type(exc).__name__


async def _extract_cab_file(
    cab_path: Path,
    out_dir: Path,
    expected_name: str,
    settings: Settings,
    exec_fn: Any = None,
) -> Path | None:
    """Safely extract a single target file from a Microsoft CAB archive (.pd_ / .dl_)."""
    try:
        with cab_path.open("rb") as fh:
            if fh.read(len(CAB_MAGIC)) != CAB_MAGIC:
                return None
    except OSError:
        return None

    if exec_fn is None and shutil.which(settings.CABEXTRACT_BIN) is None:
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [settings.CABEXTRACT_BIN, "-q", "-d", str(out_dir), str(cab_path)]
    exec_callable = exec_fn or asyncio.create_subprocess_exec
    try:
        proc = await exec_callable(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return None

    try:
        await asyncio.wait_for(
            proc.communicate(),
            timeout=min(float(settings.MS_SYMBOL_FETCH_TIMEOUT_SECONDS), 30.0),
        )
    except (TimeoutError, asyncio.CancelledError) as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return None

    if proc.returncode != 0:
        return None

    out_root = out_dir.resolve()
    expected_low = expected_name.lower()
    fallback_file: Path | None = None
    for item in out_dir.rglob("*"):
        if item.is_symlink() or not item.is_file():
            continue
        try:
            resolved = item.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(out_root):
            continue
        if item.name.lower() == expected_low:
            return resolved
        if fallback_file is None:
            fallback_file = resolved
    return fallback_file


async def _download_symsrv_artifact(
    *,
    client: httpx.AsyncClient,
    symsrv_urls: list[str],
    filename: str,
    symbol_id: str,
    staging_dir: Path,
    max_bytes: int,
    expected_magics: tuple[bytes, ...],
    timeout_seconds: float,
    settings: Settings,
    exec_fn: Any = None,
) -> tuple[Path | None, str, str]:
    """Download an uncompressed or CAB-compressed artifact from configured SymSrv URLs.

    Returns (local_path, outcome, detail) where outcome is one of:
    'ok', 'not_found', 'too_large', 'invalid', 'network_error'.
    """
    compressed = _compressed_symsrv_name(filename)
    saw_network_error = False
    last_error = ""

    for base_url in symsrv_urls:
        raw_dest = staging_dir / filename
        raw_url = _build_symsrv_url(base_url, filename, symbol_id, filename)
        status, detail = await _stream_download_to_file(
            client, raw_url, raw_dest, max_bytes, timeout_seconds
        )
        if status == "ok":
            if _check_file_magic(raw_dest, expected_magics):
                return raw_dest, "ok", ""
            raw_dest.unlink(missing_ok=True)
            last_error = "invalid file magic"
            continue
        if status == "too_large":
            return None, "too_large", detail
        if status == "network_error":
            saw_network_error = True
            last_error = detail
            continue

        # Raw file returned 404: try CAB-compressed variant (.pd_ / .dl_ / .ex_)
        if compressed and (exec_fn is not None or shutil.which(settings.CABEXTRACT_BIN)):
            cab_dest = staging_dir / compressed
            cab_url = _build_symsrv_url(base_url, filename, symbol_id, compressed)
            cab_status, cab_detail = await _stream_download_to_file(
                client, cab_url, cab_dest, max_bytes, timeout_seconds
            )
            if cab_status == "ok":
                extracted = await _extract_cab_file(
                    cab_dest,
                    staging_dir / f"cab_{Path(filename).stem}",
                    filename,
                    settings,
                    exec_fn=exec_fn,
                )
                cab_dest.unlink(missing_ok=True)
                if extracted is not None and _check_file_magic(extracted, expected_magics):
                    if extracted.stat().st_size > max_bytes:
                        extracted.unlink(missing_ok=True)
                        return None, "too_large", f"Extracted size exceeds {max_bytes} bytes"
                    target = staging_dir / filename
                    if extracted != target:
                        shutil.move(str(extracted), str(target))
                    return target, "ok", ""
                last_error = "invalid cab payload"
            elif cab_status == "too_large":
                return None, "too_large", cab_detail
            elif cab_status == "network_error":
                saw_network_error = True
                last_error = cab_detail

    if saw_network_error:
        return None, "network_error", last_error
    if last_error:
        return None, "invalid", last_error
    return None, "not_found", "not found on configured SymSrv URLs"


def _public_cache_has_room(storage: StorageManager, additional_bytes: int = 0) -> bool:
    if storage.is_host_floor_breached():
        return False
    quota = storage.get_partition_quota_bytes("symbols/public_cache")
    if quota <= 0:
        return False
    used, _ = storage.partition_used_bytes("symbols/public_cache")
    return (used + max(0, additional_bytes)) <= quota


async def _import_single_candidate(
    *,
    candidate: WindowsSymbolCandidate,
    symsrv_urls: list[str],
    client: httpx.AsyncClient,
    storage: StorageManager,
    settings: Settings,
    project_id: uuid.UUID | str | None,
    exec_fn: Any,
    cab_exec_fn: Any,
) -> str:
    """Import one Windows system symbol into symbols/public_cache.

    Returns one of: 'imported', 'cached_hit', 'not_found', 'skipped', 'network_error', 'failed'.
    """
    if not _is_valid_pdb_identity(candidate.debug_file, candidate.debug_id):
        return "skipped"
    if not is_windows_system_module(
        filename=candidate.filename,
        code_file=candidate.code_file,
        module_name=candidate.module_name,
        debug_file=candidate.debug_file,
    ):
        return "skipped"

    if has_cached_symbol(storage, candidate.debug_file, candidate.debug_id, project_id=project_id):
        return "cached_hit"
    if is_negative_cached(storage, candidate.debug_file, candidate.debug_id):
        return "skipped"
    if is_circuit_open(storage):
        return "skipped"
    if not _public_cache_has_room(storage):
        logger.warning(
            "Skipping MSDL import for %s: public_cache quota or host floor reached",
            candidate.debug_file,
        )
        return "skipped"

    lock_name = f"msdl-{candidate.debug_id.lower()[:40]}"
    async with storage.shared_lock(lock_name):
        # Re-check after acquiring per-symbol lock so concurrent workers dedupe cleanly.
        if has_cached_symbol(
            storage, candidate.debug_file, candidate.debug_id, project_id=project_id
        ):
            return "cached_hit"
        if is_negative_cached(storage, candidate.debug_file, candidate.debug_id):
            return "skipped"
        if is_circuit_open(storage) or not _public_cache_has_room(storage):
            return "skipped"

        timeout_budget = max(5.0, float(settings.MS_SYMBOL_FETCH_TIMEOUT_SECONDS))
        max_pdb_bytes = max(1, int(settings.MS_SYMBOL_MAX_PDB_MB)) * 1024 * 1024
        max_pe_bytes = max(1, int(settings.MS_SYMBOL_MAX_PE_MB)) * 1024 * 1024
        neg_ttl = max(0, int(settings.MS_SYMBOL_NEGATIVE_CACHE_HOURS)) * 3600

        with storage.staging_workspace("msdl") as staging_dir:
            pdb_path, pdb_outcome, pdb_detail = await _download_symsrv_artifact(
                client=client,
                symsrv_urls=symsrv_urls,
                filename=candidate.debug_file,
                symbol_id=candidate.debug_id,
                staging_dir=staging_dir,
                max_bytes=max_pdb_bytes,
                expected_magics=PDB_MAGICS,
                timeout_seconds=timeout_budget,
                settings=settings,
                exec_fn=cab_exec_fn,
            )

            if pdb_outcome == "network_error":
                tripped = _record_circuit_network_failure(storage, pdb_detail)
                logger.warning(
                    "MSDL network error for %s/%s: %s (circuit_open=%s)",
                    candidate.debug_file,
                    candidate.debug_id,
                    pdb_detail,
                    tripped,
                )
                return "network_error"

            if pdb_outcome == "not_found":
                _record_circuit_success(storage)
                _write_negative_cache(
                    storage, candidate.debug_file, candidate.debug_id, "not_found", neg_ttl
                )
                return "not_found"

            if pdb_outcome != "ok" or pdb_path is None:
                _record_circuit_success(storage)
                _write_negative_cache(
                    storage,
                    candidate.debug_file,
                    candidate.debug_id,
                    pdb_outcome,
                    min(neg_ttl, TRANSIENT_MISS_TTL_SECONDS) if neg_ttl else 0,
                )
                return "failed"

            _record_circuit_success(storage)

            # Best-effort PE download for x64/arm64 CFI unwind rules (never blocks PDB import).
            pe_path: Path | None = None
            if settings.MS_SYMBOL_FETCH_PE_UNWIND:
                pe_id = _is_valid_pe_identity(candidate.code_file, candidate.code_id)
                if pe_id is not None:
                    pe_name, code_id = pe_id
                    pe_dir = staging_dir / "pe"
                    pe_dir.mkdir(exist_ok=True)
                    pe_candidate, pe_outcome, _ = await _download_symsrv_artifact(
                        client=client,
                        symsrv_urls=symsrv_urls,
                        filename=pe_name,
                        symbol_id=code_id,
                        staging_dir=pe_dir,
                        max_bytes=max_pe_bytes,
                        expected_magics=(PE_MAGIC,),
                        timeout_seconds=min(10.0, timeout_budget),
                        settings=settings,
                        exec_fn=cab_exec_fn,
                    )
                    if pe_outcome == "ok" and pe_candidate is not None:
                        pe_path = pe_candidate

            # Convert PDB (+ optional PE) into Breakpad .sym via dump_syms.
            try:
                sym_bytes, _ = await run_dump_syms(
                    pdb_path=pdb_path,
                    pe_path=pe_path,
                    settings=settings,
                    exec_fn=exec_fn,
                )
            except SymbolConversionError:
                if pe_path is None:
                    _write_negative_cache(
                        storage,
                        candidate.debug_file,
                        candidate.debug_id,
                        "conversion_failed",
                        min(neg_ttl, TRANSIENT_MISS_TTL_SECONDS) if neg_ttl else 0,
                    )
                    logger.warning(
                        "dump_syms failed for MSDL PDB %s/%s",
                        candidate.debug_file,
                        candidate.debug_id,
                        exc_info=True,
                    )
                    return "failed"
                try:
                    sym_bytes, _ = await run_dump_syms(
                        pdb_path=pdb_path,
                        pe_path=None,
                        settings=settings,
                        exec_fn=exec_fn,
                    )
                except SymbolConversionError:
                    _write_negative_cache(
                        storage,
                        candidate.debug_file,
                        candidate.debug_id,
                        "conversion_failed",
                        min(neg_ttl, TRANSIENT_MISS_TTL_SECONDS) if neg_ttl else 0,
                    )
                    logger.warning(
                        "dump_syms PDB-only fallback failed for %s/%s",
                        candidate.debug_file,
                        candidate.debug_id,
                        exc_info=True,
                    )
                    return "failed"

            try:
                header = parse_sym_header(sym_bytes.decode("utf-8", errors="replace"))
            except SymbolConversionError:
                _write_negative_cache(
                    storage,
                    candidate.debug_file,
                    candidate.debug_id,
                    "invalid_sym_header",
                    min(neg_ttl, TRANSIENT_MISS_TTL_SECONDS) if neg_ttl else 0,
                )
                return "failed"

            if header.debug_id.upper() != candidate.debug_id.upper():
                logger.warning(
                    "MSDL symbol debug_id mismatch for %s: expected %s got %s",
                    candidate.debug_file,
                    candidate.debug_id,
                    header.debug_id,
                )
                _write_negative_cache(
                    storage,
                    candidate.debug_file,
                    candidate.debug_id,
                    "debug_id_mismatch",
                    min(neg_ttl, TRANSIENT_MISS_TTL_SECONDS) if neg_ttl else 0,
                )
                return "failed"

            if not _public_cache_has_room(storage, additional_bytes=len(sym_bytes)):
                return "skipped"

            rel_sym_path = _public_cache_sym_rel(candidate.debug_file, candidate.debug_id)
            _resolve_in_partition(storage, "symbols/public_cache", rel_sym_path)
            await asyncio.to_thread(storage.atomic_write_bytes, rel_sym_path, sym_bytes)
            logger.info(
                "Imported Windows system symbol from MSDL: %s (%d bytes)",
                rel_sym_path,
                len(sym_bytes),
            )
            return "imported"


async def fetch_and_import_windows_symbols(
    candidates: Iterable[WindowsSymbolCandidate],
    storage: StorageManager,
    settings: Settings,
    *,
    project_id: uuid.UUID | str | None = None,
    exec_fn: Any = None,
    cab_exec_fn: Any = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> MSFetchSummary:
    """Download missing Windows system symbols from MSDL, convert via dump_syms, and cache."""
    summary = MSFetchSummary()
    if not settings.ENABLE_PUBLIC_SYMBOLS or not settings.MS_SYMBOL_AUTO_FETCH:
        return summary

    _, symsrv_urls = split_public_symbol_urls(settings.PUBLIC_SYMBOL_URLS)
    if not symsrv_urls:
        return summary

    if is_circuit_open(storage):
        summary.circuit_open = True
        return summary

    candidate_list = list(candidates)
    if not candidate_list:
        return summary

    sem = asyncio.Semaphore(MAX_MS_FETCH_CONCURRENCY)
    client_kwargs: dict[str, Any] = {
        "follow_redirects": True,
        "headers": {"User-Agent": SYMSRV_USER_AGENT},
    }
    if http_transport is not None:
        client_kwargs["transport"] = http_transport

    async with httpx.AsyncClient(**client_kwargs) as client:

        async def _worker(cand: WindowsSymbolCandidate) -> tuple[WindowsSymbolCandidate, str]:
            async with sem:
                if is_circuit_open(storage):
                    return cand, "circuit_open"
                try:
                    outcome = await _import_single_candidate(
                        candidate=cand,
                        symsrv_urls=symsrv_urls,
                        client=client,
                        storage=storage,
                        settings=settings,
                        project_id=project_id,
                        exec_fn=exec_fn,
                        cab_exec_fn=cab_exec_fn,
                    )
                    return cand, outcome
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "Unexpected error importing MSDL symbol %s/%s",
                        cand.debug_file,
                        cand.debug_id,
                        exc_info=True,
                    )
                    return cand, "failed"

        results = await asyncio.gather(*(_worker(c) for c in candidate_list))

    for cand, outcome in results:
        if outcome == "circuit_open":
            summary.circuit_open = True
            summary.skipped += 1
        elif outcome == "imported":
            summary.attempted += 1
            summary.imported += 1
            summary.imported_keys.append(f"{cand.debug_file}/{cand.debug_id}")
        elif outcome == "cached_hit":
            summary.cached_hit += 1
        elif outcome == "not_found":
            summary.attempted += 1
            summary.not_found += 1
        elif outcome == "network_error":
            summary.attempted += 1
            summary.failed += 1
        elif outcome == "failed":
            summary.attempted += 1
            summary.failed += 1
        else:
            summary.skipped += 1

    if is_circuit_open(storage):
        summary.circuit_open = True
    return summary


__all__ = [
    "CIRCUIT_COOLDOWN_SECONDS",
    "CIRCUIT_FAILURE_THRESHOLD",
    "MSFetchSummary",
    "SYMSRV_USER_AGENT",
    "WindowsSymbolCandidate",
    "fetch_and_import_windows_symbols",
    "has_cached_symbol",
    "is_circuit_open",
    "is_negative_cached",
    "is_windows_system_module",
    "select_windows_system_candidates",
    "split_public_symbol_urls",
]
