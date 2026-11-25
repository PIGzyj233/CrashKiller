"""Non-blocking Microsoft Symbol Server (MSDL/SymSrv) fetcher and Breakpad .sym importer."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import ntpath
import os
import re
import shutil
import struct
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.services.settings_service import DEFAULT_NOISE_MODULES
from app.services.storage import StorageError, StorageManager
from app.services.symbol_service import (
    SymbolConversionError,
    _parse_sym_records,
    _resolve_in_partition,
    _validate_path_segment,
    parse_sym_header,
    run_dump_syms,
    store_public_symbol,
)

logger = logging.getLogger(__name__)

SYMSRV_USER_AGENT = "Microsoft-Symbol-Server/10.0.10522.521"
SYMSRV_SCHEME_PREFIX = "symsrv+"

CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 600
TRANSIENT_MISS_TTL_SECONDS = 3600
MAX_MS_FETCH_CONCURRENCY = 2
DOWNLOAD_CHUNK_BYTES = 65536
MAX_WINBINDEX_PE_PROBES = 8
PORTABLE_AGE = 0xFFFFFFFF

MISS_CACHE_DIR_REL = "symbols/public_cache/_msdl_miss"
CIRCUIT_STATE_REL = "symbols/public_cache/_msdl_circuit.json"

MSF7_MAGIC = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\0\0\0"
PDB_MAGICS: tuple[bytes, ...] = (
    b"Microsoft C/C++ MSF 7.00\r\n\x1aDS",
    b"Microsoft C/C++ program database 2.00\r\n\x1aJG",
    b"BSJB",
)
PE_MAGIC = b"MZ"
CAB_MAGIC = b"MSCF"

WINBINDEX_BASE_URL = "https://winbindex.m417z.com/data/by_filename_compressed"

_DEBUG_ID_RE = re.compile(r"^[0-9A-Fa-f]{33,40}$")
_CODE_ID_RE = re.compile(r"^[0-9A-Fa-f]{8,24}$")
_ALL_ZERO_RE = re.compile(r"^0+$")
_DASHED_GUID_RE = r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}"
_MANUAL_GUID_RE = re.compile(
    rf"^({_DASHED_GUID_RE}|\{{{_DASHED_GUID_RE}\}}|[0-9A-Fa-f]{{32}})([0-9A-Fa-f]{{0,8}})$"
)
_INVALID_MANUAL_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_WINDOWS_SYSTEM_PATH_RE = re.compile(
    r"^(?:[a-z]:/windows/"
    r"(?:system32|syswow64|winsxs|systemapps|microsoft\.net|"
    r"servicing|immersivecontrolpanel|systemresources|assembly)/|"
    r"[a-z]:/program files/windowsapps/microsoft\.)",
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
        "shfolder.dll",
        "comctl32.dll",
        "comdlg32.dll",
        "gdiplus.dll",
        "oleacc.dll",
        "uiautomationcore.dll",
        "winspool.drv",
        "msimg32.dll",
        "usp10.dll",
        "normaliz.dll",
        "urlmon.dll",
        "iertutil.dll",
        "ws2_32.dll",
        "wsock32.dll",
        "winhttp.dll",
        "wininet.dll",
        "webio.dll",
        "httpapi.dll",
        "websocket.dll",
        "crypt32.dll",
        "cryptsp.dll",
        "cryptnet.dll",
        "cryptbase.dll",
        "cryptui.dll",
        "cryptngc.dll",
        "certcli.dll",
        "bcrypt.dll",
        "bcryptprimitives.dll",
        "ncrypt.dll",
        "ncryptsslp.dll",
        "ntasn1.dll",
        "msasn1.dll",
        "rsaenh.dll",
        "schannel.dll",
        "dpapi.dll",
        "wintrust.dll",
        "wldp.dll",
        "secur32.dll",
        "sspicli.dll",
        "authz.dll",
        "gpapi.dll",
        "mskeyprotect.dll",
        "tbs.dll",
        "dbghelp.dll",
        "dbgcore.dll",
        "imagehlp.dll",
        "wer.dll",
        "faultrep.dll",
        "imm32.dll",
        "uxtheme.dll",
        "dwmapi.dll",
        "dwmcore.dll",
        "dcomp.dll",
        "dataexchange.dll",
        "graphicscapture.dll",
        "coremessaging.dll",
        "coreuicomponents.dll",
        "twinapi.dll",
        "twinapi.appcore.dll",
        "twinui.appcore.dll",
        "twinui.pcshell.dll",
        "rometadata.dll",
        "winsta.dll",
        "textinputframework.dll",
        "textshaping.dll",
        "inputhost.dll",
        "globinputhost.dll",
        "directmanipulation.dll",
        "directxdatabasehelper.dll",
        "msctf.dll",
        "bcp47langs.dll",
        "bcp47mrm.dll",
        "powrprof.dll",
        "umpdc.dll",
        "setupapi.dll",
        "cfgmgr32.dll",
        "devobj.dll",
        "drvstore.dll",
        "hid.dll",
        "winusb.dll",
        "bluetoothapis.dll",
        "winmm.dll",
        "winmmbase.dll",
        "avrt.dll",
        "mmdevapi.dll",
        "audioses.dll",
        "wdmaud.drv",
        "midimap.dll",
        "msacm32.dll",
        "ksuser.dll",
        "avicap32.dll",
        "msvfw32.dll",
        "mswsock.dll",
        "dnsapi.dll",
        "dnsrslvr.dll",
        "iphlpapi.dll",
        "dhcpcsvc.dll",
        "dhcpcsvc6.dll",
        "fwpuclnt.dll",
        "nlaapi.dll",
        "nsi.dll",
        "winnsi.dll",
        "napinsp.dll",
        "pnrpnsp.dll",
        "winrnr.dll",
        "wshbth.dll",
        "wshunix.dll",
        "rasadhlp.dll",
        "rasapi32.dll",
        "rasman.dll",
        "rtutils.dll",
        "netprofm.dll",
        "npmproxy.dll",
        "ondemandconnroutehelper.dll",
        "wlanapi.dll",
        "wlanutil.dll",
        "wcmsvc.dll",
        "netapi32.dll",
        "netutils.dll",
        "wkscli.dll",
        "srvcli.dll",
        "samcli.dll",
        "logoncli.dll",
        "mpr.dll",
        "cscapi.dll",
        "davclnt.dll",
        "ntlanman.dll",
        "wkssvc.dll",
        "wldap32.dll",
        "winscard.dll",
        "sensapi.dll",
        "ntmarta.dll",
        "version.dll",
        "psapi.dll",
        "wtsapi32.dll",
        "userenv.dll",
        "profapi.dll",
        "propsys.dll",
        "wintypes.dll",
        "kernel.appcore.dll",
        "clbcatq.dll",
        "resourcepolicyclient.dll",
        "policymanager.dll",
        "rmclient.dll",
        "apphelp.dll",
        "sxs.dll",
        "msi.dll",
        "cabinet.dll",
        "xmllite.dll",
        "msxml6.dll",
        "msxml3.dll",
        "webservices.dll",
        "fltlib.dll",
        "pdh.dll",
        "wevtapi.dll",
        "wbemprox.dll",
        "wbemcomn.dll",
        "wbemsvc.dll",
        "fastprox.dll",
        "wmiutils.dll",
        "taskschd.dll",
        "sfc.dll",
        "sfc_os.dll",
        "linkinfo.dll",
        "ntshrui.dll",
        "edputil.dll",
        "cldapi.dll",
        "slc.dll",
        "sppc.dll",
        "d3d8.dll",
        "d3d9.dll",
        "d3d10.dll",
        "d3d10_1.dll",
        "d3d10warp.dll",
        "d3d11.dll",
        "d3d11on12.dll",
        "d3d12.dll",
        "d3d12core.dll",
        "d3dcompiler_47.dll",
        "ddraw.dll",
        "dinput8.dll",
        "dsound.dll",
        "dxva2.dll",
        "dxgi.dll",
        "dxcore.dll",
        "d2d1.dll",
        "dwrite.dll",
        "dxil.dll",
        "dxcompiler.dll",
        "directml.dll",
        "opengl32.dll",
        "glu32.dll",
        "wincodec.dll",
        "windowscodecs.dll",
        "mscms.dll",
        "coloradapterclient.dll",
        "mf.dll",
        "mfplat.dll",
        "mfcore.dll",
        "mfreadwrite.dll",
        "mfsrcsnk.dll",
        "mftranscode.dll",
        "evr.dll",
        "quartz.dll",
        "devenum.dll",
        "xinput1_4.dll",
        "xinput1_3.dll",
        "xaudio2_9.dll",
        "clr.dll",
        "clrjit.dll",
        "coreclr.dll",
        "mscoree.dll",
        "mscoreei.dll",
        "mscorwks.dll",
        "fusion.dll",
        "diasymreader.dll",
        "msvcp_win.dll",
        "msvcp110_win.dll",
        "msvcp140_2.dll",
        "msvcp140_atomic_wait.dll",
        "msvcp140_codecvt_ids.dll",
        "vcruntime140_threads.dll",
        "ucrtbase_clr0400.dll",
        "msvcr120.dll",
        "msvcp120.dll",
        "msvcr120_clr0400.dll",
        "msvcp120_clr0400.dll",
        "msvcr110.dll",
        "msvcp110.dll",
        "msvcr100.dll",
        "msvcp100.dll",
        "vccorlib140.dll",
        "mfc140u.dll",
        "mfc140.dll",
        "rtworkq.dll",
        "d3d9on12.dll",
        "d3d11_3.dll",
        "dxgidebug.dll",
        "dbgeng.dll",
        "dbgmodel.dll",
        "symsrv.dll",
        "olepro32.dll",
        "riched20.dll",
        "msftedit.dll",
        "credui.dll",
        "kerberos.dll",
        "msv1_0.dll",
        "wdigest.dll",
        "duser.dll",
        "dui70.dll",
        "explorerframe.dll",
        "actxprxy.dll",
        "esent.dll",
        "mlang.dll",
        "compkgsup.dll",
        "msvproc.dll",
        "colorcnv.dll",
    }
)

KNOWN_WINDOWS_MODULE_PREFIXES: tuple[str, ...] = (
    "api-ms-win-",
    "ext-ms-win-",
    "windows.",
    "microsoft.ui.",
    "microsoft.internal.",
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
    "twinapi",
    "staterepository.",
    "appxdeployment",
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


_ABS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _basename(path_str: str) -> str:
    cleaned = path_str.replace("\\", "/").rstrip("/")
    return cleaned.rsplit("/", 1)[-1] if cleaned else ""


def _is_safe_segment(value: str) -> bool:
    try:
        _validate_path_segment(value, "segment")
    except SymbolConversionError:
        return False
    return True


def normalize_debug_file_name(raw_debug_file: str) -> str:
    """Strip clean absolute Windows/POSIX paths to basename while keeping traversal strings raw."""
    cleaned = raw_debug_file.strip()
    if not cleaned or "\x00" in cleaned:
        return cleaned
    if "/" not in cleaned and "\\" not in cleaned:
        return cleaned
    # Only strip clean absolute paths (e.g. C:\Windows\System32\ntdll.pdb) without '..' segments;
    # leave relative/hostile paths (like ../evil.pdb) untouched so _is_safe_segment rejects them.
    is_abs = bool(_ABS_DRIVE_PATH_RE.match(cleaned)) or cleaned.startswith("/")
    if not is_abs:
        return cleaned
    parts = [p for p in cleaned.replace("\\", "/").split("/") if p]
    if any(p == ".." or ".." in p for p in parts):
        return cleaned
    leaf = parts[-1] if parts else ""
    return leaf if _is_safe_segment(leaf) else cleaned


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
    pdb_base = _basename(debug_file).strip().lower()
    if pdb_base.endswith(".pdb") and len(pdb_base) > 4:
        names.append(f"{pdb_base[:-4]}.dll")

    for name in names:
        if name in KNOWN_WINDOWS_SYSTEM_MODULES:
            return True
        if any(name.startswith(prefix) for prefix in KNOWN_WINDOWS_MODULE_PREFIXES):
            return True

    return False


def _is_valid_pdb_identity(debug_file: str, debug_id: str) -> bool:
    if not debug_file or debug_file != debug_file.strip():
        return False
    if len(debug_file) <= 4 or not debug_file.lower().endswith(".pdb"):
        return False
    if (
        len(debug_file) > 255
        or not _is_safe_segment(debug_file)
        or _INVALID_MANUAL_NAME_RE.search(debug_file)
    ):
        return False
    if not debug_id or not _DEBUG_ID_RE.match(debug_id):
        return False
    if _ALL_ZERO_RE.match(debug_id) or _ALL_ZERO_RE.match(debug_id[:32]):
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


def _is_microsoft_symsrv_url(raw_url: str) -> bool:
    parsed = urlparse(raw_url.strip())
    host = (parsed.hostname or "").lower()
    return host == "msdl.microsoft.com" or host.endswith(".microsoft.com")


def _normalize_pe_code_ids(code_id: str) -> list[str]:
    """Return SymSrv PE key candidates ordered by MSDL canonical format first.

    Microsoft's SymSrv expects `{timestamp:08X}{image_size:x}` (8 uppercase hex chars
    for timestamp + lowercase hex image_size without leading zeros).
    """
    cid = code_id.strip()
    if not cid:
        return []
    upper = cid.upper()
    if re.fullmatch(r"[0-9A-Fa-f]{9,16}", cid):
        size_val = int(cid[8:], 16)
        if size_val > 0:
            canonical = f"{cid[:8].upper()}{size_val:x}"
            if canonical != upper:
                return [canonical, upper]
            return [canonical]
    return [upper]


def _parse_age_value(age: str | int) -> int:
    if isinstance(age, bool):
        raise ValueError("Invalid PDB age")
    if isinstance(age, int):
        val = age
    else:
        cleaned = str(age).strip()
        if not cleaned:
            raise ValueError("PDB age must not be empty")
        val = int(cleaned, 16 if cleaned.lower().startswith("0x") else 10)
    if not 0 <= val <= PORTABLE_AGE:
        raise ValueError("PDB age out of range")
    return val


def parse_manual_ms_target(
    module_or_pdb: str,
    debug_id_or_guid: str,
    *,
    age: str | int | None = None,
    code_file: str | None = None,
    code_id: str | None = None,
) -> WindowsSymbolCandidate:
    """Parse a user-supplied module/PDB name and Debug ID / GUID into a sync candidate.

    Accepts inputs like `GraphicsCapture.dll` + `90D28435EAAB109449C47402C53C42D31`
    or `GraphicsCapture.pdb` + dashed GUID + optional age.
    """
    raw_name = _basename(module_or_pdb.strip())
    if not raw_name or _INVALID_MANUAL_NAME_RE.search(raw_name):
        raise ValueError("Invalid module or PDB filename")

    low_name = raw_name.lower()
    if low_name.endswith(".pdb"):
        stem = raw_name[:-4]
        debug_file = raw_name
        derived_code_file = f"{stem}.dll" if stem else None
        module_name = derived_code_file or raw_name
    elif low_name.endswith((".dll", ".exe")):
        stem = raw_name[:-4]
        debug_file = f"{stem}.pdb"
        derived_code_file = raw_name
        module_name = raw_name
    else:
        stem = raw_name
        debug_file = f"{stem}.pdb"
        derived_code_file = f"{stem}.dll"
        module_name = derived_code_file

    if not stem or not _is_safe_segment(debug_file):
        raise ValueError("Invalid module or PDB filename")

    match = _MANUAL_GUID_RE.fullmatch(debug_id_or_guid.strip())
    if not match:
        raise ValueError(
            "debug_id must be a 32-hex/dashed GUID optionally followed by a hex age"
        )
    parsed_guid = uuid.UUID(match.group(1))
    if parsed_guid.int == 0:
        raise ValueError("GUID must not be all zeros")

    inline_age_hex = match.group(2)
    inline_age = int(inline_age_hex, 16) if inline_age_hex else None
    explicit_age = _parse_age_value(age) if age is not None and str(age).strip() != "" else None
    if inline_age is not None and explicit_age is not None and inline_age != explicit_age:
        raise ValueError("Provided age conflicts with the age suffix in debug_id")
    final_age = (
        inline_age
        if inline_age is not None
        else (explicit_age if explicit_age is not None else 1)
    )
    debug_id = f"{parsed_guid.hex.upper()}{final_age:X}"

    resolved_code_file = (
        _basename(code_file.strip())
        if code_file and code_file.strip()
        else derived_code_file
    )
    if resolved_code_file and (
        _INVALID_MANUAL_NAME_RE.search(resolved_code_file)
        or not _is_safe_segment(resolved_code_file)
    ):
        raise ValueError("Invalid code_file name")

    resolved_code_id: str | None = None
    if code_id is not None and code_id.strip():
        cid = code_id.strip()
        if not _CODE_ID_RE.match(cid) or _ALL_ZERO_RE.match(cid):
            raise ValueError("Invalid code_id (expected hex timestamp + image size)")
        resolved_code_id = cid

    return WindowsSymbolCandidate(
        module_name=module_name,
        debug_file=debug_file,
        debug_id=debug_id,
        code_file=resolved_code_file,
        code_id=resolved_code_id,
        filename=resolved_code_file,
    )


def _find_local_sym_in_root(root: Path, debug_file: str, debug_id: str) -> Path | None:
    if not root.exists():
        return None
    base_df = normalize_debug_file_name(debug_file)
    if not _is_safe_segment(base_df) or not _is_safe_segment(debug_id):
        return None
    stem = Path(base_df).stem or base_df
    candidate_leaves = [f"{stem}.sym"]
    full_leaf = f"{base_df}.sym"
    if full_leaf.lower() != candidate_leaves[0].lower():
        candidate_leaves.append(full_leaf)

    upper_did = debug_id.upper()
    for leaf in candidate_leaves:
        exact = root / base_df / upper_did / leaf
        if exact.is_file():
            return exact

    # Case-insensitive fallback for Linux hosts storing Windows PDB names
    df_low = base_df.lower()
    did_low = debug_id.lower()
    sym_lows = {leaf.lower() for leaf in candidate_leaves}
    try:
        for df_entry in root.iterdir():
            if not df_entry.is_dir() or df_entry.name.lower() != df_low:
                continue
            for id_entry in df_entry.iterdir():
                if not id_entry.is_dir() or id_entry.name.lower() != did_low:
                    continue
                for file_entry in id_entry.iterdir():
                    if file_entry.is_file() and file_entry.name.lower() in sym_lows:
                        return file_entry
    except OSError:
        return None
    return None


def _has_local_sym_in_root(root: Path, debug_file: str, debug_id: str) -> bool:
    return _find_local_sym_in_root(root, debug_file, debug_id) is not None


def _sym_file_has_unwind(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return _parse_sym_records(fh).has_unwind_records
    except (OSError, SymbolConversionError):
        return False


def has_cached_symbol_detail(
    storage: StorageManager,
    debug_file: str,
    debug_id: str,
    project_id: uuid.UUID | str | None = None,
) -> tuple[bool, str | None, Path | None]:
    """Return (exists, scope, path) for a Breakpad .sym in project symbols or public_cache."""
    if project_id is not None:
        proj_seg = str(project_id).strip()
        if _is_safe_segment(proj_seg):
            try:
                proj_root = storage.resolve_path(f"symbols/sym/{proj_seg}")
            except StorageError:
                proj_root = None
            if proj_root is not None:
                found_proj = _find_local_sym_in_root(proj_root, debug_file, debug_id)
                if found_proj is not None:
                    return True, "project", found_proj
    pub_root = storage.partition_path("symbols/public_cache")
    found_pub = _find_local_sym_in_root(pub_root, debug_file, debug_id)
    if found_pub is not None:
        return True, "public", found_pub
    return False, None, None


def has_cached_symbol(
    storage: StorageManager,
    debug_file: str,
    debug_id: str,
    project_id: uuid.UUID | str | None = None,
) -> bool:
    """Return True if a Breakpad .sym already exists in project symbols or public_cache."""
    found, _, _ = has_cached_symbol_detail(storage, debug_file, debug_id, project_id=project_id)
    return found


def _miss_cache_rel(debug_file: str, debug_id: str) -> str:
    digest = hashlib.sha256(f"{debug_file.lower()}:{debug_id.upper()}".encode()).hexdigest()[:32]
    return f"{MISS_CACHE_DIR_REL}/{digest}.json"


def get_negative_cache_info(
    storage: StorageManager, debug_file: str, debug_id: str
) -> dict[str, Any] | None:
    """Return active negative-cache payload if present and unexpired, else None."""
    rel = _miss_cache_rel(debug_file, debug_id)
    try:
        path = storage.resolve_path(rel)
    except StorageError:
        return None
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        expires_at = float(payload.get("expires_at", 0.0))
    except (OSError, ValueError, TypeError):
        return None
    if time.time() < expires_at:
        return payload if isinstance(payload, dict) else {"reason": "unknown"}
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def is_negative_cached(storage: StorageManager, debug_file: str, debug_id: str) -> bool:
    """Return True when (debug_file, debug_id) has an unexpired negative-cache entry."""
    return get_negative_cache_info(storage, debug_file, debug_id) is not None


def clear_negative_cache(
    storage: StorageManager,
    debug_file: str | None = None,
    debug_id: str | None = None,
) -> int:
    """Clear either one (debug_file, debug_id) negative-cache entry or the entire miss cache."""
    if debug_file and debug_id:
        rel = _miss_cache_rel(debug_file, debug_id)
        try:
            path = storage.resolve_path(rel)
            if path.is_file():
                path.unlink(missing_ok=True)
                return 1
        except (StorageError, OSError):
            pass
        return 0
    try:
        miss_dir = storage.resolve_path(MISS_CACHE_DIR_REL)
    except StorageError:
        return 0
    if not miss_dir.is_dir():
        return 0
    removed = 0
    try:
        for item in miss_dir.iterdir():
            if item.is_file():
                try:
                    item.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed


def reset_circuit_breaker(storage: StorageManager) -> None:
    """Reset any open MSDL circuit breaker state."""
    _record_circuit_success(storage)


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


def _read_native_pdb_metadata(data: bytes) -> _PdbMetadata | None:
    if len(data) < 56:
        return None
    block_size, _, block_count, directory_size, _, block_map = struct.unpack_from("<6I", data, 32)
    if not block_size or block_size > 1024 * 1024 or len(data) != block_size * block_count:
        return None

    def block_list(buffer: bytes, offset: int, size: int) -> tuple[int, ...]:
        count = (size + block_size - 1) // block_size
        if offset < 0 or offset + count * 4 > len(buffer):
            raise ValueError("block list out of bounds")
        return struct.unpack_from(f"<{count}I", buffer, offset)

    def read_blocks(blocks: tuple[int, ...], size: int) -> bytes:
        chunks: list[bytes] = []
        for idx in blocks:
            start = idx * block_size
            end = start + block_size
            if end > len(data):
                raise ValueError("block out of bounds")
            chunks.append(data[start:end])
        return b"".join(chunks)[:size]

    directory_blocks = block_list(data, block_map * block_size, directory_size)
    directory = read_blocks(directory_blocks, directory_size)
    if len(directory) < 4:
        return None
    (count,) = struct.unpack_from("<I", directory)
    if count == 0 or 4 + count * 4 > len(directory):
        return None
    sizes = struct.unpack_from(f"<{count}I", directory, 4)
    stream_blocks: list[tuple[int, ...]] = []
    offset = 4 + count * 4
    for size in sizes:
        blocks = () if size == 0xFFFFFFFF else block_list(directory, offset, size)
        stream_blocks.append(blocks)
        offset += len(blocks) * 4

    def stream(index: int) -> bytes:
        if index >= count or sizes[index] == 0xFFFFFFFF:
            return b""
        return read_blocks(stream_blocks[index], sizes[index])

    s1 = stream(1)
    if len(s1) < 28:
        return None
    signature, age, guid_bytes = struct.unpack_from("<II16s", s1, 4)
    guid = uuid.UUID(bytes_le=guid_bytes)
    dbi = stream(3)
    if len(dbi) < 64:
        return _PdbMetadata(guid, age, signature)

    (age,) = struct.unpack_from("<I", dbi, 8)
    (machine,) = struct.unpack_from("<H", dbi, 58)
    mod, contrib, section_map, source, types, _, debug_size, ec = struct.unpack_from("<8I", dbi, 24)
    image_size = 0
    if debug_size >= 12:
        debug_start = 64 + mod + contrib + section_map + source + types + ec
        if debug_start + 12 <= len(dbi):
            (section_stream,) = struct.unpack_from("<H", dbi, debug_start + 10)
            sections = stream(section_stream)
            end_rva = 0
            for sec_off in range(0, len(sections) - 39, 40):
                virtual_size, rva, raw_size = struct.unpack_from("<III", sections, sec_off + 8)
                end_rva = max(end_rva, rva + (virtual_size or raw_size))
            if end_rva > 0:
                image_size = (end_rva + 0xFFF) & ~0xFFF
    return _PdbMetadata(guid, age, signature, machine=machine, image_size=image_size)


def _read_pdb_metadata(path: Path) -> _PdbMetadata | None:
    """Extract GUID/age/signature/image_size from MSF 7.00 or Portable PDB files."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        if data.startswith(MSF7_MAGIC):
            return _read_native_pdb_metadata(data)
        if data.startswith(b"BSJB") and len(data) >= 24:
            (version_size,) = struct.unpack_from("<I", data, 12)
            offset = (16 + version_size + 3) & ~3
            _, count = struct.unpack_from("<HH", data, offset)
            offset += 4
            for _ in range(count):
                start, size = struct.unpack_from("<II", data, offset)
                end = data.index(b"\0", offset + 8)
                if data[offset + 8 : end] == b"#Pdb" and size >= 20 and start + 20 <= len(data):
                    guid_bytes, stamp = struct.unpack_from("<16sI", data, start)
                    return _PdbMetadata(uuid.UUID(bytes_le=guid_bytes), 1, stamp, portable=True)
                offset = (end + 4) & ~3
    except (ValueError, struct.error, IndexError):
        return None
    return None


def _pe_matches_pdb(
    pe_path: Path,
    pdb_name: str,
    pdb_meta: _PdbMetadata,
    expected_key: str | None = None,
) -> bool:
    """Verify a downloaded PE binary's CodeView RSDS record matches the downloaded PDB."""
    try:
        data = pe_path.read_bytes()
    except OSError:
        return False
    if len(data) < 0x40 or data[:2] != b"MZ":
        return False
    try:
        (pe,) = struct.unpack_from("<I", data, 0x3C)
        if pe + 24 > len(data) or data[pe : pe + 4] != b"PE\0\0":
            return False
        _, sections, stamp, _, _, optional_size, _ = struct.unpack_from("<HHIIIHH", data, pe + 4)
        optional = pe + 24
        if optional + optional_size > len(data) or optional_size < 64:
            return False
        (magic,) = struct.unpack_from("<H", data, optional)
        directory_start = {0x10B: 96, 0x20B: 112}.get(magic)
        if directory_start is None or optional_size < directory_start + 56:
            return False
        image_size, headers_size = struct.unpack_from("<II", data, optional + 56)
        if expected_key and f"{stamp:08X}{image_size:x}".lower() != expected_key.lower():
            return False
        (directory_count,) = struct.unpack_from("<I", data, optional + directory_start - 4)
        if directory_count < 7:
            return False
        debug_rva, debug_size = struct.unpack_from("<II", data, optional + directory_start + 48)
        if not debug_rva or debug_size < 28:
            return False

        debug_offset = debug_rva if debug_rva + debug_size <= headers_size else None
        section_start = optional + optional_size
        for index in range(sections):
            sec_off = section_start + index * 40
            if sec_off + 40 > len(data):
                break
            _, rva, raw_size, raw_offset = struct.unpack_from("<4I", data, sec_off + 8)
            if rva <= debug_rva and debug_rva + debug_size <= rva + raw_size:
                debug_offset = raw_offset + debug_rva - rva
                break
        if debug_offset is None or debug_offset + debug_size > len(data):
            return False

        for entry in range(debug_offset, debug_offset + debug_size - 27, 28):
            _, debug_stamp, _, minor, kind, size, _, pointer = struct.unpack_from(
                "<IIHHIIII", data, entry
            )
            if kind != 2 or size < 25 or not pointer or pointer + size > len(data):
                continue
            record = data[pointer : pointer + size]
            if record[:4] != b"RSDS" or record[4:20] != pdb_meta.guid.bytes_le:
                continue
            (age,) = struct.unpack_from("<I", record, 20)
            name = ntpath.basename(record[24:].split(b"\0", 1)[0].decode("utf-8", "replace"))
            if name.casefold() != pdb_name.casefold() or age != pdb_meta.age:
                continue
            if not pdb_meta.portable or (minor == 0x504D and debug_stamp == pdb_meta.signature):
                return True
    except (struct.error, ValueError):
        return False
    return False


async def _fetch_winbindex_pe_keys(
    client: httpx.AsyncClient,
    pe_name: str,
    pdb_meta: _PdbMetadata,
    timeout_seconds: float,
) -> list[str]:
    """Best-effort query of Winbindex compressed indexes to discover PE timestamp+size keys."""
    name = pe_name.lower()
    encoded = quote(name, safe="")
    name_hash = 5381
    for ch in name:
        name_hash = (name_hash * 33 + ord(ch)) & 0xFF
    urls = (
        f"{WINBINDEX_BASE_URL}/{encoded}.json.gz",
        f"https://m417z.com/winbindex-data-arm64/by_filename_compressed/{encoded}.json.gz",
        f"https://m417z.com/winbindex-data-insider/by_filename_compressed/{name_hash:02x}/{encoded}.json.gz",
    )
    req_timeout = httpx.Timeout(
        connect=min(4.0, timeout_seconds),
        read=min(8.0, timeout_seconds),
        write=4.0,
        pool=4.0,
    )
    candidates: dict[str, tuple[bool, bool, bool]] = {}
    for url in urls:
        try:
            async with asyncio.timeout(min(8.0, timeout_seconds)):
                resp = await client.get(
                    url,
                    headers={"User-Agent": SYMSRV_USER_AGENT},
                    timeout=req_timeout,
                )
            if resp.status_code != 200 or not resp.content:
                continue
            payload = json.loads(gzip.decompress(resp.content))
            if not isinstance(payload, dict):
                continue
        except Exception:
            continue

        for item in payload.values():
            if not isinstance(item, dict):
                continue
            info = item.get("fileInfo")
            if not isinstance(info, dict):
                continue
            timestamp = info.get("timestamp")
            if not isinstance(timestamp, int):
                continue
            size = info.get("virtualSize")
            sizes: Iterable[int]
            if isinstance(size, int) and size > 0:
                sizes = (size,)
            elif (
                isinstance(info.get("lastSectionVirtualAddress"), int)
                and isinstance(info.get("lastSectionPointerToRawData"), int)
                and isinstance(info.get("size"), int)
            ):
                last_rva = info["lastSectionVirtualAddress"]
                last_size = info["size"] - info["lastSectionPointerToRawData"]
                first = (last_rva + last_size + 0xFFF) & ~0xFFF
                sizes = range(first, max(last_rva, first - 0x4000), -0x1000)
            else:
                sizes = (pdb_meta.image_size,) if pdb_meta.image_size else ()

            for sz in sizes:
                if sz <= 0:
                    continue
                key = f"{timestamp:08X}{sz:x}"
                rank = (
                    not pdb_meta.portable and timestamp == pdb_meta.signature,
                    sz == pdb_meta.image_size,
                    info.get("machineType") == pdb_meta.machine,
                )
                if key not in candidates or rank > candidates[key]:
                    candidates[key] = rank

    ranked = sorted(candidates, key=lambda k: candidates[k], reverse=True)
    return ranked[:MAX_WINBINDEX_PE_PROBES]


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

    use_windows_expand = False
    if exec_fn is None and shutil.which(settings.CABEXTRACT_BIN) is None:
        if os.name == "nt":
            sys_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            expand_exe = sys_root / "System32" / "expand.exe"
            if expand_exe.is_file():
                use_windows_expand = True
            else:
                return None
        else:
            return None

    out_dir.mkdir(parents=True, exist_ok=True)
    if use_windows_expand:
        sys_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        expand_exe = sys_root / "System32" / "expand.exe"
        cmd = [str(expand_exe), str(cab_path), str(out_dir / expected_name)]
    else:
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


def _can_extract_cab(settings: Settings, exec_fn: Any) -> bool:
    if exec_fn is not None or shutil.which(settings.CABEXTRACT_BIN):
        return True
    if os.name == "nt":
        expand_exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "expand.exe"
        return expand_exe.is_file()
    return False


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
    saw_network_error = False
    last_error = ""
    cab_supported = _can_extract_cab(settings, exec_fn)

    for base_url in symsrv_urls:
        name_variants = (
            [filename.lower(), filename]
            if _is_microsoft_symsrv_url(base_url) and filename.lower() != filename
            else [filename]
        )

        for req_name in name_variants:
            raw_dest = staging_dir / filename
            raw_url = _build_symsrv_url(base_url, req_name, symbol_id, req_name)
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
            compressed = _compressed_symsrv_name(req_name)
            if compressed and cab_supported:
                cab_dest = staging_dir / compressed
                cab_url = _build_symsrv_url(base_url, req_name, symbol_id, compressed)
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


async def _download_matching_pe(
    *,
    candidate: WindowsSymbolCandidate,
    pdb_path: Path,
    symsrv_urls: list[str],
    client: httpx.AsyncClient,
    staging_dir: Path,
    max_pe_bytes: int,
    timeout_budget: float,
    settings: Settings,
    cab_exec_fn: Any,
    allow_winbindex: bool,
) -> Path | None:
    """Best-effort download of a matching PE binary (.dll/.exe) for CFI unwind records."""
    pe_dir = staging_dir / "pe"
    pe_dir.mkdir(exist_ok=True)

    pe_name: str | None = None
    pe_id = _is_valid_pe_identity(candidate.code_file, candidate.code_id)
    tried_keys: set[str] = set()

    if pe_id is not None:
        pe_name, raw_code_id = pe_id
        for key in _normalize_pe_code_ids(raw_code_id):
            tried_keys.add(key.lower())
            pe_candidate, pe_outcome, _ = await _download_symsrv_artifact(
                client=client,
                symsrv_urls=symsrv_urls,
                filename=pe_name,
                symbol_id=key,
                staging_dir=pe_dir,
                max_bytes=max_pe_bytes,
                expected_magics=(PE_MAGIC,),
                timeout_seconds=min(10.0, timeout_budget),
                settings=settings,
                exec_fn=cab_exec_fn,
            )
            if pe_outcome == "ok" and pe_candidate is not None:
                return pe_candidate

    if pe_name is None:
        for raw_name in (candidate.code_file, candidate.filename, candidate.module_name):
            if not raw_name:
                continue
            base = _basename(raw_name).strip()
            if base.lower().endswith((".dll", ".exe")) and _is_safe_segment(base):
                pe_name = base
                break
        if pe_name is None and candidate.debug_file.lower().endswith(".pdb"):
            stem = Path(candidate.debug_file).stem
            if stem and _is_safe_segment(f"{stem}.dll"):
                pe_name = f"{stem}.dll"

    if pe_name is None:
        return None

    pdb_meta = await asyncio.to_thread(_read_pdb_metadata, pdb_path)
    if pdb_meta is None:
        return None

    derived_keys: list[str] = []
    if not pdb_meta.portable and pdb_meta.image_size > 0:
        derived_key = f"{pdb_meta.signature:08X}{pdb_meta.image_size:x}"
        if derived_key.lower() not in tried_keys:
            derived_keys.append(derived_key)

    for key in derived_keys:
        tried_keys.add(key.lower())
        pe_candidate, pe_outcome, _ = await _download_symsrv_artifact(
            client=client,
            symsrv_urls=symsrv_urls,
            filename=pe_name,
            symbol_id=key,
            staging_dir=pe_dir,
            max_bytes=max_pe_bytes,
            expected_magics=(PE_MAGIC,),
            timeout_seconds=min(10.0, timeout_budget),
            settings=settings,
            exec_fn=cab_exec_fn,
        )
        if pe_outcome == "ok" and pe_candidate is not None:
            if await asyncio.to_thread(
                _pe_matches_pdb, pe_candidate, candidate.debug_file, pdb_meta, key
            ):
                return pe_candidate
            pe_candidate.unlink(missing_ok=True)

    if allow_winbindex and settings.MS_SYMBOL_ENABLE_WINBINDEX_FALLBACK:
        wb_keys = await _fetch_winbindex_pe_keys(client, pe_name, pdb_meta, timeout_budget)
        for key in wb_keys:
            if key.lower() in tried_keys:
                continue
            tried_keys.add(key.lower())
            pe_candidate, pe_outcome, _ = await _download_symsrv_artifact(
                client=client,
                symsrv_urls=symsrv_urls,
                filename=pe_name,
                symbol_id=key,
                staging_dir=pe_dir,
                max_bytes=max_pe_bytes,
                expected_magics=(PE_MAGIC,),
                timeout_seconds=min(8.0, timeout_budget),
                settings=settings,
                exec_fn=cab_exec_fn,
            )
            if pe_outcome == "ok" and pe_candidate is not None:
                if await asyncio.to_thread(
                    _pe_matches_pdb, pe_candidate, candidate.debug_file, pdb_meta, key
                ):
                    return pe_candidate
                pe_candidate.unlink(missing_ok=True)

    return None


async def _persist_public_msdl_symbol(
    *,
    session: AsyncSession | None,
    session_factory: Any,
    db_lock: asyncio.Lock | None,
    storage: StorageManager,
    settings: Settings,
    sym_bytes: bytes,
    header: Any,
    staged_pdb: Path | None,
    staged_pe: Path | None,
) -> tuple[uuid.UUID | None, bool]:
    """Persist converted MSDL .sym into DB (when session/factory available) and disk."""
    if session is not None:
        if db_lock is not None:
            async with db_lock:
                res = await store_public_symbol(
                    session,
                    storage,
                    settings,
                    sym_bytes=sym_bytes,
                    header=header,
                    source="msdl",
                    staged_pdb=staged_pdb,
                    staged_pe=staged_pe,
                )
        else:
            res = await store_public_symbol(
                session,
                storage,
                settings,
                sym_bytes=sym_bytes,
                header=header,
                source="msdl",
                staged_pdb=staged_pdb,
                staged_pe=staged_pe,
            )
        return res.symbol.id, res.upgraded

    if session_factory is not None:
        async def _write_with_own_session() -> tuple[uuid.UUID | None, bool]:
            async with session_factory() as own_session:
                res = await store_public_symbol(
                    own_session,
                    storage,
                    settings,
                    sym_bytes=sym_bytes,
                    header=header,
                    source="msdl",
                    staged_pdb=staged_pdb,
                    staged_pe=staged_pe,
                )
                return res.symbol.id, res.upgraded

        if db_lock is not None:
            async with db_lock:
                return await _write_with_own_session()
        return await _write_with_own_session()

    rel_sym_path = _public_cache_sym_rel(header.debug_file, header.debug_id)
    _resolve_in_partition(storage, "symbols/public_cache", rel_sym_path)
    await asyncio.to_thread(storage.atomic_write_bytes, rel_sym_path, sym_bytes)
    return None, False


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
    session: AsyncSession | None = None,
    session_factory: Any = None,
    db_lock: asyncio.Lock | None = None,
    explicit_targets: bool = False,
    force: bool = False,
    allow_winbindex: bool = False,
) -> MSFetchItemResult:
    """Import one Windows/public symbol into symbols/public_cache (and DB if provided)."""

    def _result(
        status: str,
        detail: str | None = None,
        *,
        has_pe_unwind: bool = False,
        symbol_id: uuid.UUID | None = None,
    ) -> MSFetchItemResult:
        return MSFetchItemResult(
            module_name=candidate.module_name,
            debug_file=candidate.debug_file,
            debug_id=candidate.debug_id,
            status=status,
            detail=detail,
            has_pe_unwind=has_pe_unwind,
            symbol_id=symbol_id,
        )

    if not _is_valid_pdb_identity(candidate.debug_file, candidate.debug_id):
        return _result("skipped", "Invalid PDB filename or debug_id")
    if not explicit_targets and not is_windows_system_module(
        filename=candidate.filename,
        code_file=candidate.code_file,
        module_name=candidate.module_name,
        debug_file=candidate.debug_file,
    ):
        return _result("skipped", "Not recognized as a Windows system module")

    if force:
        clear_negative_cache(storage, candidate.debug_file, candidate.debug_id)

    cached, cached_scope, cached_path = has_cached_symbol_detail(
        storage, candidate.debug_file, candidate.debug_id, project_id=project_id
    )
    existing_has_unwind = (
        await asyncio.to_thread(_sym_file_has_unwind, cached_path)
        if cached and cached_path is not None
        else False
    )
    if cached and (
        not force
        or cached_scope == "project"
        or existing_has_unwind
        or not settings.MS_SYMBOL_FETCH_PE_UNWIND
    ):
        return _result(
            "cached_hit",
            "Already cached locally",
            has_pe_unwind=existing_has_unwind,
        )

    if not force and is_negative_cached(storage, candidate.debug_file, candidate.debug_id):
        return _result("skipped", "Negative-cached from recent miss")
    if not force and is_circuit_open(storage):
        return _result("skipped", "MSDL circuit breaker is open")
    if not _public_cache_has_room(storage):
        logger.warning(
            "Skipping MSDL import for %s: public_cache quota or host floor reached",
            candidate.debug_file,
        )
        return _result("skipped", "Public symbol cache quota or host disk floor reached")

    was_upgrade = cached and cached_scope == "public" and not existing_has_unwind
    lock_name = f"msdl-{candidate.debug_id.lower()[:40]}"
    async with storage.shared_lock(lock_name):
        # Re-check after acquiring per-symbol lock so concurrent workers dedupe cleanly.
        cached, cached_scope, cached_path = has_cached_symbol_detail(
            storage, candidate.debug_file, candidate.debug_id, project_id=project_id
        )
        existing_has_unwind = (
            await asyncio.to_thread(_sym_file_has_unwind, cached_path)
            if cached and cached_path is not None
            else False
        )
        if cached and (
            not force
            or cached_scope == "project"
            or existing_has_unwind
            or not settings.MS_SYMBOL_FETCH_PE_UNWIND
        ):
            return _result(
                "cached_hit",
                "Already cached locally",
                has_pe_unwind=existing_has_unwind,
            )
        if not force and is_negative_cached(storage, candidate.debug_file, candidate.debug_id):
            return _result("skipped", "Negative-cached from recent miss")
        if (not force and is_circuit_open(storage)) or not _public_cache_has_room(storage):
            return _result("skipped", "Skipped due to circuit breaker or storage quota")

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
                return _result("network_error", pdb_detail or "MSDL network error")

            if pdb_outcome == "not_found":
                _record_circuit_success(storage)
                _write_negative_cache(
                    storage, candidate.debug_file, candidate.debug_id, "not_found", neg_ttl
                )
                return _result("not_found", pdb_detail or "Not found on Microsoft Symbol Server")

            if pdb_outcome != "ok" or pdb_path is None:
                _record_circuit_success(storage)
                _write_negative_cache(
                    storage,
                    candidate.debug_file,
                    candidate.debug_id,
                    pdb_outcome,
                    min(neg_ttl, TRANSIENT_MISS_TTL_SECONDS) if neg_ttl else 0,
                )
                return _result("failed", pdb_detail or pdb_outcome)

            _record_circuit_success(storage)

            # Best-effort PE download for x64/arm64 CFI unwind rules (never blocks PDB import).
            pe_path: Path | None = None
            if settings.MS_SYMBOL_FETCH_PE_UNWIND:
                pe_path = await _download_matching_pe(
                    candidate=candidate,
                    pdb_path=pdb_path,
                    symsrv_urls=symsrv_urls,
                    client=client,
                    staging_dir=staging_dir,
                    max_pe_bytes=max_pe_bytes,
                    timeout_budget=timeout_budget,
                    settings=settings,
                    cab_exec_fn=cab_exec_fn,
                    allow_winbindex=allow_winbindex,
                )

            if was_upgrade and pe_path is None:
                clear_negative_cache(storage, candidate.debug_file, candidate.debug_id)
                return _result(
                    "cached_hit",
                    "Already cached locally (PE unwind unavailable)",
                    has_pe_unwind=False,
                )

            # Convert PDB (+ optional PE) into Breakpad .sym via dump_syms.
            pe_used = False
            try:
                sym_bytes, pe_used = await run_dump_syms(
                    pdb_path=pdb_path,
                    pe_path=pe_path,
                    settings=settings,
                    exec_fn=exec_fn,
                )
            except SymbolConversionError as exc:
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
                    return _result("failed", str(exc) or "dump_syms conversion failed")
                try:
                    sym_bytes, pe_used = await run_dump_syms(
                        pdb_path=pdb_path,
                        pe_path=None,
                        settings=settings,
                        exec_fn=exec_fn,
                    )
                except SymbolConversionError as exc2:
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
                    return _result("failed", str(exc2) or "dump_syms PDB-only fallback failed")

            try:
                header = parse_sym_header(sym_bytes.decode("utf-8", errors="replace"))
            except SymbolConversionError as exc:
                _write_negative_cache(
                    storage,
                    candidate.debug_file,
                    candidate.debug_id,
                    "invalid_sym_header",
                    min(neg_ttl, TRANSIENT_MISS_TTL_SECONDS) if neg_ttl else 0,
                )
                return _result("failed", str(exc) or "Invalid Breakpad .sym header")

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
                return _result(
                    "failed",
                    f"debug_id mismatch: expected {candidate.debug_id}, got {header.debug_id}",
                )

            if not _public_cache_has_room(storage, additional_bytes=len(sym_bytes)):
                return _result("skipped", "Public symbol cache quota or host disk floor reached")

            # Preserve requested debug_file casing if header only differs by case
            if (
                header.debug_file.lower() == candidate.debug_file.lower()
                and header.debug_file != candidate.debug_file
                and _is_safe_segment(candidate.debug_file)
            ):
                cand_stem = Path(candidate.debug_file).stem or candidate.debug_file
                if _is_safe_segment(cand_stem):
                    header.debug_file = candidate.debug_file
                    header.stem = cand_stem

            sym_id, db_upgraded = await _persist_public_msdl_symbol(
                session=session,
                session_factory=session_factory,
                db_lock=db_lock,
                storage=storage,
                settings=settings,
                sym_bytes=sym_bytes,
                header=header,
                staged_pdb=pdb_path,
                staged_pe=pe_path if pe_used and header.has_unwind_records else None,
            )
            clear_negative_cache(storage, candidate.debug_file, candidate.debug_id)
            if was_upgrade and not header.has_unwind_records and not db_upgraded:
                return _result(
                    "cached_hit",
                    "Already cached locally (PE unwind unavailable)",
                    has_pe_unwind=False,
                    symbol_id=sym_id,
                )
            is_upgraded = db_upgraded or (was_upgrade and header.has_unwind_records)
            status_label = "upgraded" if is_upgraded else "imported"
            logger.info(
                "%s Windows/public symbol from MSDL: %s/%s (%d bytes, pe_unwind=%s)",
                status_label.capitalize(),
                header.debug_file,
                header.debug_id,
                len(sym_bytes),
                header.has_unwind_records,
            )
            status_detail = (
                "Upgraded with PE unwind records"
                if status_label == "upgraded"
                else "Imported from MSDL"
            )
            return _result(
                status_label,
                status_detail,
                has_pe_unwind=header.has_unwind_records,
                symbol_id=sym_id,
            )


async def _invoke_callback(callback: Any, arg: Any) -> None:
    if callback is None:
        return
    maybe_coro = callback(arg)
    if maybe_coro is not None and asyncio.iscoroutine(maybe_coro):
        await maybe_coro


async def fetch_and_import_windows_symbols(
    candidates: Iterable[WindowsSymbolCandidate],
    storage: StorageManager,
    settings: Settings,
    *,
    project_id: uuid.UUID | str | None = None,
    session: AsyncSession | None = None,
    session_factory: Any = None,
    explicit_targets: bool = False,
    force: bool = False,
    bypass_auto_fetch_flag: bool = False,
    allow_winbindex: bool = False,
    max_concurrency: int = MAX_MS_FETCH_CONCURRENCY,
    on_item_start: Callable[[WindowsSymbolCandidate], Awaitable[None] | None] | None = None,
    on_item_done: Callable[[MSFetchItemResult], Awaitable[None] | None] | None = None,
    exec_fn: Any = None,
    cab_exec_fn: Any = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> MSFetchSummary:
    """Download missing Windows/public symbols from MSDL, convert via dump_syms, and store."""
    summary = MSFetchSummary()
    candidate_list = list(candidates)
    if not bypass_auto_fetch_flag and (
        not settings.ENABLE_PUBLIC_SYMBOLS or not settings.MS_SYMBOL_AUTO_FETCH
    ):
        return summary

    _, symsrv_urls = split_public_symbol_urls(settings.PUBLIC_SYMBOL_URLS)
    if not symsrv_urls:
        if bypass_auto_fetch_flag:
            for cand in candidate_list:
                summary.skipped += 1
                item = MSFetchItemResult(
                    module_name=cand.module_name,
                    debug_file=cand.debug_file,
                    debug_id=cand.debug_id,
                    status="skipped",
                    detail="No SymSrv/MSDL URL configured in PUBLIC_SYMBOL_URLS",
                )
                summary.items.append(item)
                await _invoke_callback(on_item_done, item)
        return summary

    if force:
        # Clear any stale circuit-breaker cooldown before an explicit user retry;
        # consecutive network errors during this batch will still trip it fresh.
        reset_circuit_breaker(storage)
    elif is_circuit_open(storage):
        summary.circuit_open = True
        for cand in candidate_list:
            summary.skipped += 1
            item = MSFetchItemResult(
                module_name=cand.module_name,
                debug_file=cand.debug_file,
                debug_id=cand.debug_id,
                status="skipped",
                detail="MSDL circuit breaker is temporarily open",
            )
            summary.items.append(item)
            await _invoke_callback(on_item_done, item)
        return summary

    if not candidate_list:
        return summary

    sem = asyncio.Semaphore(max(1, int(max_concurrency)))
    db_lock = asyncio.Lock() if (session is not None or session_factory is not None) else None
    client_kwargs: dict[str, Any] = {
        "follow_redirects": True,
        "headers": {"User-Agent": SYMSRV_USER_AGENT},
    }
    if http_transport is not None:
        client_kwargs["transport"] = http_transport

    async with httpx.AsyncClient(**client_kwargs) as client:

        async def _worker(cand: WindowsSymbolCandidate) -> MSFetchItemResult:
            async with sem:
                if is_circuit_open(storage):
                    circuit_item = MSFetchItemResult(
                        module_name=cand.module_name,
                        debug_file=cand.debug_file,
                        debug_id=cand.debug_id,
                        status="skipped",
                        detail="MSDL circuit breaker opened during batch",
                    )
                    await _invoke_callback(on_item_done, circuit_item)
                    return MSFetchItemResult(
                        module_name=cand.module_name,
                        debug_file=cand.debug_file,
                        debug_id=cand.debug_id,
                        status="circuit_open",
                        detail="MSDL circuit breaker opened during batch",
                    )
                await _invoke_callback(on_item_start, cand)
                try:
                    res = await _import_single_candidate(
                        candidate=cand,
                        symsrv_urls=symsrv_urls,
                        client=client,
                        storage=storage,
                        settings=settings,
                        project_id=project_id,
                        exec_fn=exec_fn,
                        cab_exec_fn=cab_exec_fn,
                        session=session,
                        session_factory=session_factory,
                        db_lock=db_lock,
                        explicit_targets=explicit_targets,
                        force=force,
                        allow_winbindex=allow_winbindex,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Unexpected error importing MSDL symbol %s/%s",
                        cand.debug_file,
                        cand.debug_id,
                        exc_info=True,
                    )
                    res = MSFetchItemResult(
                        module_name=cand.module_name,
                        debug_file=cand.debug_file,
                        debug_id=cand.debug_id,
                        status="failed",
                        detail=str(exc) or type(exc).__name__,
                    )
                await _invoke_callback(on_item_done, res)
                return res

        results = await asyncio.gather(*(_worker(c) for c in candidate_list))

    for item in results:
        outcome = item.status
        if outcome == "circuit_open":
            summary.circuit_open = True
            summary.skipped += 1
            summary.items.append(
                MSFetchItemResult(
                    module_name=item.module_name,
                    debug_file=item.debug_file,
                    debug_id=item.debug_id,
                    status="skipped",
                    detail=item.detail,
                )
            )
            continue

        summary.items.append(item)
        if outcome == "imported":
            summary.attempted += 1
            summary.imported += 1
            summary.imported_keys.append(f"{item.debug_file}/{item.debug_id}")
        elif outcome == "upgraded":
            summary.attempted += 1
            summary.upgraded += 1
            summary.imported_keys.append(f"{item.debug_file}/{item.debug_id}")
        elif outcome == "cached_hit":
            summary.cached_hit += 1
        elif outcome == "not_found":
            summary.attempted += 1
            summary.not_found += 1
        elif outcome in {"network_error", "failed"}:
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
