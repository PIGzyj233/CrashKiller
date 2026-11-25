"""Subprocess execution, PE CodeView identity probing, and dump_syms conversion."""

from __future__ import annotations

import asyncio
import os
import struct
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from app.config import Settings
from app.services.symbol_service.models import (
    MAX_SYMBOL_OUTPUT_BYTES,
    MAX_SYMBOL_TOOL_MEMORY_BYTES,
    SymbolConversionError,
)
from app.services.symbol_service.parsing import _module_identity, parse_sym_header


async def _run_subprocess(
    cmd: list[str],
    timeout_seconds: int,
    exec_fn: Any = None,
) -> tuple[int, bytes, bytes]:
    if exec_fn is None:
        native_cmd = cmd
        if sys.platform == "linux":
            native_cmd = [
                "prlimit",
                f"--as={MAX_SYMBOL_TOOL_MEMORY_BYTES}",
                f"--fsize={MAX_SYMBOL_OUTPUT_BYTES}",
                "--",
                *cmd,
            ]
        with (
            tempfile.TemporaryFile(dir=Path(cmd[1]).parent) as stdout_file,
            tempfile.TemporaryFile(dir=Path(cmd[1]).parent) as stderr_file,
        ):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *native_cmd, stdout=stdout_file, stderr=stderr_file
                )
            except OSError as exc:
                raise SymbolConversionError(f"Failed to start {cmd[0]}: {exc}") from exc
            try:
                rc = await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
            except (TimeoutError, asyncio.CancelledError) as exc:
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise SymbolConversionError(
                    f"dump_syms timed out after {timeout_seconds}s"
                ) from exc
            if os.fstat(stdout_file.fileno()).st_size >= MAX_SYMBOL_OUTPUT_BYTES:
                raise SymbolConversionError("dump_syms output exceeds the 128 MiB conversion limit")
            stdout_file.seek(0)
            stderr_file.seek(0)
            return rc, stdout_file.read(), stderr_file.read(16384)

    try:
        proc = await exec_fn(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise SymbolConversionError(f"Symbol tool binary not found: {cmd[0]}") from exc
    except OSError as exc:
        raise SymbolConversionError(f"Failed to start {cmd[0]}: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_seconds,
        )
    except (TimeoutError, asyncio.CancelledError) as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise SymbolConversionError(
            f"dump_syms timed out after {timeout_seconds}s: {' '.join(cmd)}"
        ) from exc

    rc = proc.returncode if proc.returncode is not None else -1
    return rc, stdout or b"", stderr or b""


def _pe_debug_identity(path: Path) -> tuple[str, str] | None:
    """Read the PE CodeView GUID and age without loading the executable into memory."""
    with path.open("rb") as stream:
        dos_header = stream.read(64)
        if len(dos_header) != 64 or dos_header[:2] != b"MZ":
            return None
        stream.seek(struct.unpack_from("<I", dos_header, 60)[0])
        coff_header = stream.read(24)
        if len(coff_header) != 24 or coff_header[:4] != b"PE\0\0":
            return None
        section_count, timestamp = struct.unpack_from("<HI", coff_header, 6)
        optional_size = struct.unpack_from("<H", coff_header, 20)[0]
        optional_header = stream.read(optional_size)
        if optional_header[:2] == b"\x0b\x01":
            directories_offset = 96
        elif optional_header[:2] == b"\x0b\x02":
            directories_offset = 112
        else:
            return None
        debug_offset = directories_offset + 6 * 8
        if len(optional_header) < debug_offset + 8:
            return None
        directory_count = struct.unpack_from("<I", optional_header, directories_offset - 4)[0]
        if directory_count <= 6:
            return None
        debug_rva, debug_size = struct.unpack_from("<II", optional_header, debug_offset)
        if not debug_rva or not 28 <= debug_size <= 65536:
            return None
        image_size, header_size = struct.unpack_from("<II", optional_header, 56)
        debug_position = debug_rva if debug_rva + debug_size <= header_size else None
        if debug_position is None:
            for _ in range(section_count):
                section = stream.read(40)
                if len(section) < 40:
                    return None
                section_rva, file_size, file_offset = struct.unpack_from("<III", section, 12)
                delta = debug_rva - section_rva
                if 0 <= delta and delta + debug_size <= file_size:
                    debug_position = file_offset + delta
                    break
        if debug_position is None:
            return None
        stream.seek(debug_position)
        table = stream.read(debug_size)
        for offset in range(0, len(table) - 27, 28):
            record_type, record_size, _, record_position = struct.unpack_from(
                "<IIII", table, offset + 12
            )
            if record_type != 2 or record_size < 24:
                continue
            stream.seek(record_position)
            record = stream.read(24)
            if len(record) == 24 and record[:4] == b"RSDS":
                guid = uuid.UUID(bytes_le=record[4:20]).hex.upper()
                age = struct.unpack_from("<I", record, 20)[0]
                return f"{guid}{age:X}", f"{timestamp:08X}{image_size:X}"
    return None


async def run_dump_syms(
    pdb_path: Path | None,
    pe_path: Path | None,
    settings: Settings,
    exec_fn: Any = None,
) -> tuple[bytes, bool]:
    """Run dump_syms with [pe, pdb] and automatic [pdb] fallback, returning (sym_bytes, pe_used)."""
    if pdb_path is None and pe_path is None:
        raise SymbolConversionError("At least one of pdb_path or pe_path must be provided")

    timeout = settings.TOOL_TIMEOUT_SECONDS
    bin_path = settings.DUMP_SYMS_BIN

    async def convert(*inputs: Path) -> tuple[int, bytes, bytes]:
        return await _run_subprocess(
            [bin_path, *(str(path) for path in inputs)],
            timeout_seconds=timeout,
            exec_fn=exec_fn,
        )

    if pe_path is not None and pdb_path is not None:
        # A PE conversion may automatically consume a same-directory PDB.
        # Compare an isolated PDB conversion with the PE's own build identifier.
        pe_identity = _pe_debug_identity(pe_path)
        with tempfile.TemporaryDirectory(prefix="probe_", dir=pe_path.parent) as directory:
            probes = Path(directory)
            pdb_probe = probes / pdb_path.name
            os.link(pdb_path, pdb_probe)
            pdb_rc, pdb_sym, pdb_err = await convert(pdb_probe)
            if pdb_rc != 0:
                detail = pdb_err.decode("utf-8", errors="replace").strip()
                raise SymbolConversionError(f"dump_syms could not read the PDB: {detail or pdb_rc}")
            pdb_header = parse_sym_header(pdb_sym.decode("utf-8", errors="replace"))
            if pe_identity is None:
                pe_dir = probes / "pe"
                pe_dir.mkdir()
                pe_probe = pe_dir / pe_path.name
                os.link(pe_path, pe_probe)
                pe_rc, pe_sym, _ = await convert(pe_probe)
                if pe_rc != 0 or b"MODULE " not in pe_sym:
                    return pdb_sym, False
                pe_header = parse_sym_header(pe_sym.decode("utf-8", errors="replace"))
                if _module_identity(pe_header) != _module_identity(pdb_header):
                    raise SymbolConversionError("PDB and PE identify different builds")
                expected_code_id = pe_header.code_id
                del pe_sym
            else:
                expected_debug_id, expected_code_id = pe_identity
                if expected_debug_id != pdb_header.debug_id:
                    raise SymbolConversionError("PDB and PE identify different builds")

        rc, stdout, _ = await convert(pe_path, pdb_path)
        if rc != 0 or b"MODULE " not in stdout:
            return pdb_sym, False
        header = parse_sym_header(stdout.decode("utf-8", errors="replace"))
        if _module_identity(header) != _module_identity(pdb_header):
            raise SymbolConversionError("Combined symbols do not identify the uploaded build")
        if expected_code_id and header.code_id != expected_code_id:
            raise SymbolConversionError("Combined symbols do not identify the uploaded PE")
        return stdout, True

    target = pe_path if pe_path is not None else pdb_path
    assert target is not None
    rc, stdout, stderr = await convert(target)
    if rc == 0 and b"MODULE " in stdout:
        return stdout, pe_path is not None

    err_msg = stderr.decode("utf-8", errors="replace").strip()
    raise SymbolConversionError(f"dump_syms failed: {err_msg or f'exit {rc}'}")
