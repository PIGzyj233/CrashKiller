"""Binary PDB/PE metadata extraction and Winbindex PE key discovery."""

from __future__ import annotations

import asyncio
import gzip
import json
import ntpath
import struct
import uuid
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import quote

import httpx

from app.services.ms_symbol_service.constants import (
    MAX_WINBINDEX_PE_PROBES,
    MSF7_MAGIC,
    SYMSRV_USER_AGENT,
    WINBINDEX_BASE_URL,
)
from app.services.ms_symbol_service.models import _PdbMetadata


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
