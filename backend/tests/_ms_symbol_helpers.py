"""Shared test constants and helpers for Microsoft Symbol Server (MSDL) tests."""

from __future__ import annotations

from pathlib import Path

NTDLL_DEBUG_ID = "7F5986E31FA943588F5488168233C87F1"
NTDLL_CODE_ID = "654321AB1F5000"
KERNELBASE_DEBUG_ID = "AABBCCDDEEFF001122334455667788991"
D3D11_DEBUG_ID = "11223344556677889900AABBCCDDEEFF2"
FAKE_PDB_BYTES = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\x00\x00\x00" + (b"\x00" * 128)
FAKE_PE_BYTES = b"MZ" + (b"\x00" * 128)
FAKE_CAB_BYTES = b"MSCF" + (b"\x00" * 128)


def _sample_sym(debug_file: str, debug_id: str, code_id: str | None = None) -> bytes:
    lines = [f"MODULE windows x86_64 {debug_id} {debug_file}"]
    if code_id:
        lines.append(f"INFO CODE_ID {code_id} {Path(debug_file).stem}.dll")
        lines.append("STACK CFI INIT 1000 40 .cfa: $rsp 8 + .ra: .cfa -8 + ^")
    lines.append("PUBLIC 1000 0 KiUserExceptionDispatcher")
    return ("\n".join(lines) + "\n").encode("utf-8")
