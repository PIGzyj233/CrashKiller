"""SymSrv HTTP artifact downloading, CAB extraction, and matching PE retrieval."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings
from app.services.ms_symbol_service.constants import (
    CAB_MAGIC,
    DOWNLOAD_CHUNK_BYTES,
    PE_MAGIC,
    SYMSRV_USER_AGENT,
)
from app.services.ms_symbol_service.identity import (
    _basename,
    _is_microsoft_symsrv_url,
    _is_safe_segment,
    _is_valid_pe_identity,
    _normalize_pe_code_ids,
)
from app.services.ms_symbol_service.models import WindowsSymbolCandidate
from app.services.ms_symbol_service.pdb_pe import (
    _fetch_winbindex_pe_keys,
    _pe_matches_pdb,
    _read_pdb_metadata,
)


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
