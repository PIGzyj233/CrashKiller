"""Unit tests for Breakpad symbol_service (dump_syms, dedup/upgrade, archiving, SymSrv)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.db.models import Project
from app.services.storage import StorageManager
from app.services.symbol_service import (
    SymbolConversionError,
    ingest_symbol,
    parse_sym_header,
    resolve_symsrv_file,
    run_dump_syms,
    stream_decompressed_zst,
)
from tests.conftest import make_disk_usage_fn, make_mock_exec

SAMPLE_SYM_WITH_CFI = (
    "MODULE windows x86_64 11223344556677889900AABBCCDDEEFF1 Game.pdb\n"
    "INFO CODE_ID 654321ABCDEF Game.exe\n"
    "FILE 0 C:\\src\\main.cpp\n"
    "FUNC 1000 40 0 main\n"
    "STACK CFI INIT 1000 40 .cfa: $rsp 8 +\n"
)

SAMPLE_SYM_PDB_ONLY = (
    "MODULE windows x86_64 AABBCCDDEEFF001122334455667788992 Engine.pdb\n"
    "FILE 0 C:\\src\\engine.cpp\n"
    "FUNC 2000 20 0 Tick\n"
)


def test_parse_sym_header_with_and_without_code_id() -> None:
    hdr = parse_sym_header(SAMPLE_SYM_WITH_CFI.lower())
    assert hdr.debug_id == "11223344556677889900AABBCCDDEEFF1"
    assert hdr.code_id == "654321ABCDEF"
    assert hdr.code_file.lower() == "game.exe"
    assert hdr.has_unwind_records is True

    hdr2 = parse_sym_header(SAMPLE_SYM_PDB_ONLY)
    assert hdr2.debug_id == "AABBCCDDEEFF001122334455667788992"
    assert hdr2.code_id is None
    assert hdr2.has_unwind_records is False

    with pytest.raises(SymbolConversionError):
        parse_sym_header("FILE 0 foo.cpp\n")


async def test_run_dump_syms_pe_pdb_and_fallback(
    tmp_path: Path,
    test_settings: Settings,
) -> None:
    pe = tmp_path / "Game.exe"
    pdb = tmp_path / "Game.pdb"
    pe.write_bytes(b"MZ")
    pdb.write_bytes(b"PDB")

    calls: list[tuple[str, ...]] = []
    exec_ok = make_mock_exec(returncode=0, stdout=SAMPLE_SYM_WITH_CFI.encode(), calls_log=calls)
    out, pe_used = await run_dump_syms(pdb, pe, test_settings, exec_fn=exec_ok)
    assert pe_used is True
    assert b"MODULE " in out
    assert calls[-1] == (test_settings.DUMP_SYMS_BIN, str(pe), str(pdb))
    assert Path(calls[0][1]).parent != Path(calls[1][1]).parent

    # Simulate PE+PDB failure followed by PDB-only fallback success
    fallback_calls: list[tuple[str, ...]] = []

    async def _fallback_exec(*cmd: str, **kwargs):
        fallback_calls.append(tuple(cmd))
        if len(cmd) == 3:
            return await make_mock_exec(returncode=1, stderr=b"unsupported pe arg")(*cmd, **kwargs)
        return await make_mock_exec(
            returncode=0,
            stdout=SAMPLE_SYM_PDB_ONLY.encode(),
        )(*cmd, **kwargs)

    out_fb, pe_used_fb = await run_dump_syms(pdb, pe, test_settings, exec_fn=_fallback_exec)
    assert pe_used_fb is False
    assert b"Engine.pdb" in out_fb
    assert len(fallback_calls) == 3

    # Timeout test
    short_settings = test_settings.model_copy(update={"TOOL_TIMEOUT_SECONDS": 0.01})
    slow_exec = make_mock_exec(returncode=0, stdout=b"", delay=0.5)
    with pytest.raises(SymbolConversionError, match="timed out"):
        await run_dump_syms(pdb, None, short_settings, exec_fn=slow_exec)


async def test_dump_syms_rejects_different_builds_and_multiple_modules(
    tmp_path: Path, test_settings: Settings
) -> None:
    pdb = tmp_path / "Game.pdb"
    pe = tmp_path / "Game.exe"
    pdb.write_bytes(b"PDB")
    pe.write_bytes(b"PE")
    calls: list[tuple[str, ...]] = []

    async def tool(*cmd: str, **kwargs):
        calls.append(cmd)
        output = SAMPLE_SYM_PDB_ONLY if Path(cmd[-1]).suffix == ".pdb" else SAMPLE_SYM_WITH_CFI
        return await make_mock_exec(returncode=0, stdout=output.encode())(*cmd, **kwargs)

    with pytest.raises(SymbolConversionError, match="different builds"):
        await run_dump_syms(pdb, pe, test_settings, exec_fn=tool)
    assert len(calls) == 2

    with pytest.raises(SymbolConversionError, match="more than one MODULE"):
        parse_sym_header(SAMPLE_SYM_WITH_CFI + SAMPLE_SYM_PDB_ONLY)


async def test_ingest_symbol_dedup_upgrade_and_raw_archiving(
    db_session: AsyncSession,
    tmp_data_dir: Path,
) -> None:
    project = Project(key="app", name="App", api_key_hash="abc")
    db_session.add(project)
    await db_session.commit()

    settings = Settings(
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        DATA_DIR=str(tmp_data_dir),
        ARCHIVE_RAW_SYMBOLS=True,
        HOST_SAFETY_FLOOR_GB=50.0,
    )
    mgr = StorageManager(
        data_dir=tmp_data_dir,
        settings=settings,
        disk_usage_fn=make_disk_usage_fn(free_gb=200.0),
    )

    pdb_only_sym = (
        b"MODULE windows x86_64 11223344556677889900AABBCCDDEEFF1 Game.pdb\n"
        b"FUNC 1000 40 0 main\n"
    )

    # 1. Initial PDB-only ingest (has_pe_unwind=False)
    res1 = await ingest_symbol(
        session=db_session,
        storage=mgr,
        settings=settings,
        project_id=project.id,
        app_version="1.0.0",
        pdb_filename="Game.pdb",
        pdb_bytes_or_path=b"RAW_PDB_DATA",
        exec_fn=make_mock_exec(returncode=0, stdout=pdb_only_sym),
    )
    assert res1.deduplicated is False
    assert res1.upgraded is False
    assert res1.symbol.has_pe_unwind is False
    assert res1.symbol.raw_archive_path is not None
    assert mgr.resolve_path(res1.symbol.sym_path).exists()
    assert mgr.resolve_path(res1.symbol.raw_archive_path).exists()

    # 2. Duplicate PDB-only ingest -> deduplicated, not upgraded
    res2 = await ingest_symbol(
        session=db_session,
        storage=mgr,
        settings=settings,
        project_id=project.id,
        app_version="1.0.0",
        pdb_filename="Game.pdb",
        pdb_bytes_or_path=b"RAW_PDB_DATA",
        exec_fn=make_mock_exec(returncode=0, stdout=pdb_only_sym),
    )
    assert res2.deduplicated is True
    assert res2.upgraded is False
    assert res2.symbol.id == res1.symbol.id

    # 3. Upgrade with PE + PDB -> deduplicated=True, upgraded=True, has_pe_unwind=True
    res3 = await ingest_symbol(
        session=db_session,
        storage=mgr,
        settings=settings,
        project_id=project.id,
        app_version="1.0.1",
        pdb_filename="Game.pdb",
        pdb_bytes_or_path=b"RAW_PDB_DATA",
        pe_filename="Game.exe",
        pe_bytes_or_path=b"RAW_PE_DATA",
        exec_fn=make_mock_exec(returncode=0, stdout=SAMPLE_SYM_WITH_CFI.encode()),
    )
    assert res3.deduplicated is True
    assert res3.upgraded is True
    assert res3.symbol.has_pe_unwind is True
    assert res3.symbol.code_id == "654321ABCDEF"

    # 4. Safety floor breached -> raw archiving skipped while .sym still saved
    breached_mgr = StorageManager(
        data_dir=tmp_data_dir,
        settings=settings,
        disk_usage_fn=make_disk_usage_fn(free_gb=30.0),
    )
    res4 = await ingest_symbol(
        session=db_session,
        storage=breached_mgr,
        settings=settings,
        project_id=project.id,
        app_version="1.0.1",
        pdb_filename="Engine.pdb",
        pdb_bytes_or_path=b"RAW_ENGINE_PDB",
        exec_fn=make_mock_exec(returncode=0, stdout=SAMPLE_SYM_PDB_ONLY.encode()),
    )
    assert res4.symbol.raw_archive_path is None
    assert res4.symbol.raw_size == 0
    assert breached_mgr.resolve_path(res4.symbol.sym_path).exists()


async def test_resolve_symsrv_file_case_insensitive_and_streaming(
    db_session: AsyncSession,
    tmp_data_dir: Path,
) -> None:
    project = Project(key="symproj", name="SymProj", api_key_hash="k")
    db_session.add(project)
    await db_session.commit()

    settings = Settings(
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        DATA_DIR=str(tmp_data_dir),
        ARCHIVE_RAW_SYMBOLS=True,
    )
    mgr = StorageManager(
        data_dir=tmp_data_dir,
        settings=settings,
        disk_usage_fn=make_disk_usage_fn(free_gb=200.0),
    )

    raw_pdb = b"MICROSOFT_PDB_CONTENT" * 100
    raw_pe = b"MZ_PE_CONTENT" * 100
    ingest_res = await ingest_symbol(
        session=db_session,
        storage=mgr,
        settings=settings,
        project_id=project.id,
        app_version="2.0.0",
        pdb_filename="Game.pdb",
        pdb_bytes_or_path=raw_pdb,
        pe_filename="Game.exe",
        pe_bytes_or_path=raw_pe,
        exec_fn=make_mock_exec(returncode=0, stdout=SAMPLE_SYM_WITH_CFI.encode()),
    )
    old_used = utcnow() - timedelta(days=2)
    ingest_res.symbol.last_used_at = old_used
    await db_session.commit()

    # Case-insensitive lookup by lowercase debug_id
    found_pdb = await resolve_symsrv_file(
        db_session,
        mgr,
        "game.pdb",
        "11223344556677889900aabbccddeeff1",
    )
    assert found_pdb is not None
    zst_pdb_path, sym_row = found_pdb
    assert sym_row.last_used_at > old_used
    assert b"".join(stream_decompressed_zst(zst_pdb_path)) == raw_pdb

    # Lookup by code_file + lowercase code_id
    found_pe = await resolve_symsrv_file(
        db_session,
        mgr,
        "GAME.EXE",
        "654321abcdef",
    )
    assert found_pe is not None
    zst_pe_path, _ = found_pe
    decompressed = b"".join(stream_decompressed_zst(zst_pe_path))
    assert decompressed == raw_pe

    # Path traversal rejected cleanly
    assert await resolve_symsrv_file(db_session, mgr, "../Game.pdb", "123") is None
    assert await resolve_symsrv_file(db_session, mgr, ".", "123") is None
    assert await resolve_symsrv_file(db_session, mgr, "Game.pdb", ".") is None


async def test_ingest_symbol_rejects_traversal_and_cross_partition_writes(
    db_session: AsyncSession,
    tmp_data_dir: Path,
) -> None:
    project = Project(key="secproj", name="SecProj", api_key_hash="k")
    db_session.add(project)
    await db_session.commit()

    settings = Settings(
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        DATA_DIR=str(tmp_data_dir),
        ARCHIVE_RAW_SYMBOLS=True,
    )
    mgr = StorageManager(
        data_dir=tmp_data_dir,
        settings=settings,
        disk_usage_fn=make_disk_usage_fn(free_gb=200.0),
    )

    malicious_headers = [
        b"MODULE windows x86_64 AAAA1111 ../etc/passwd\nFUNC 1 1 0 f\n",
        b"MODULE windows x86_64 AAAA2222 a/b.pdb\nFUNC 1 1 0 f\n",
        b"MODULE windows x86_64 AAAA3333 ../../logs/pwn.pdb\nFUNC 1 1 0 f\n",
        b"MODULE windows x86_64 ../../logs/pwn2 Safe.pdb\nFUNC 1 1 0 f\n",
        b"MODULE windows x86_64 ../../../logs/pwn3 Safe.pdb\nFUNC 1 1 0 f\n",
        (
            b"MODULE windows x86_64 AAAA4444 Safe2.pdb\n"
            b"INFO CODE_ID 123456 ../../logs/evil.exe\n"
            b"FUNC 1 1 0 f\n"
        ),
        (
            b"MODULE windows x86_64 AAAA5555 Safe3.pdb\n"
            b"INFO CODE_ID ../../logs/evil_id Safe3.exe\n"
            b"FUNC 1 1 0 f\n"
        ),
        b"MODULE windows x86_64 . Safe4.pdb\nFUNC 1 1 0 f\n",
    ]
    for sym_payload in malicious_headers:
        with pytest.raises(SymbolConversionError):
            await ingest_symbol(
                session=db_session,
                storage=mgr,
                settings=settings,
                project_id=project.id,
                app_version="1.0.0",
                pdb_filename="Safe.pdb",
                pdb_bytes_or_path=b"PDB",
                pe_filename="Safe.exe",
                pe_bytes_or_path=b"PE",
                exec_fn=make_mock_exec(returncode=0, stdout=sym_payload),
            )

    for bad_name in ("../etc/passwd", "a/b.pdb", "..\\evil.pdb", ".", "..", "bad\x00.pdb"):
        with pytest.raises(SymbolConversionError):
            await ingest_symbol(
                session=db_session,
                storage=mgr,
                settings=settings,
                project_id=project.id,
                app_version="1.0.0",
                pdb_filename=bad_name,
                pdb_bytes_or_path=b"PDB",
                exec_fn=make_mock_exec(returncode=0, stdout=SAMPLE_SYM_PDB_ONLY.encode()),
            )

    # Zero files should have been written to symbols/sym, symbols/raw, or logs
    assert list(mgr.partition_path("symbols/sym").rglob("*")) == []
    assert list(mgr.partition_path("symbols/raw").rglob("*")) == []
    assert list(mgr.partition_path("logs").rglob("*")) == []


async def test_ingest_symbol_pe_only_upgrade_preserves_pdb_raw_and_cleans_all_raw_files(
    db_session: AsyncSession,
    tmp_data_dir: Path,
) -> None:
    """PDB-only -> PE-only upgrade must retain raw PDB ref so GC/delete cleans all .zst files."""
    from app.api.v1.symbols import delete_symbol

    project = Project(key="upproj", name="UpProj", api_key_hash="k")
    db_session.add(project)
    await db_session.commit()

    settings = Settings(
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        DATA_DIR=str(tmp_data_dir),
        ARCHIVE_RAW_SYMBOLS=True,
    )
    mgr = StorageManager(
        data_dir=tmp_data_dir,
        settings=settings,
        disk_usage_fn=make_disk_usage_fn(free_gb=200.0),
    )

    pdb_only_sym = (
        b"MODULE windows x86_64 11223344556677889900AABBCCDDEEFF1 Game.pdb\n"
        b"FUNC 1000 40 0 main\n"
    )
    res_pdb = await ingest_symbol(
        session=db_session,
        storage=mgr,
        settings=settings,
        project_id=project.id,
        app_version="1.0.0",
        pdb_filename="Game.pdb",
        pdb_bytes_or_path=b"PDB_PAYLOAD",
        exec_fn=make_mock_exec(returncode=0, stdout=pdb_only_sym),
    )
    assert res_pdb.symbol.raw_archive_path == (
        "symbols/raw/11223344556677889900AABBCCDDEEFF1/Game.pdb.zst"
    )
    pdb_raw_size = res_pdb.symbol.raw_size
    assert pdb_raw_size > 0

    # Upgrade using PE-only (staged_pdb is None)
    res_up = await ingest_symbol(
        session=db_session,
        storage=mgr,
        settings=settings,
        project_id=project.id,
        app_version="1.0.1",
        pdb_filename=None,
        pdb_bytes_or_path=None,
        pe_filename="Game.exe",
        pe_bytes_or_path=b"PE_PAYLOAD",
        exec_fn=make_mock_exec(returncode=0, stdout=SAMPLE_SYM_WITH_CFI.encode()),
    )
    assert res_up.deduplicated is True
    assert res_up.upgraded is True
    assert res_up.symbol.has_pe_unwind is True
    assert res_up.symbol.raw_archive_path == (
        "symbols/raw/11223344556677889900AABBCCDDEEFF1/Game.pdb.zst"
    )
    assert res_up.symbol.raw_size > pdb_raw_size

    raw_files = sorted(
        str(p.relative_to(tmp_data_dir))
        for p in mgr.partition_path("symbols/raw").rglob("*.zst")
    )
    assert len(raw_files) == 2

    # Both PDB and PE must resolve via SymSrv
    assert (
        await resolve_symsrv_file(
            db_session, mgr, "Game.pdb", "11223344556677889900AABBCCDDEEFF1"
        )
    ) is not None
    assert await resolve_symsrv_file(db_session, mgr, "Game.exe", "654321ABCDEF") is not None

    # Delete symbol -> ALL raw .zst files must be cleaned
    await delete_symbol(symbol_id=res_up.symbol.id, session=db_session, storage=mgr)
    assert list(mgr.partition_path("symbols/raw").rglob("*.zst")) == []
    assert list(mgr.partition_path("symbols/sym").rglob("*.sym")) == []


async def test_ingest_symbol_clamps_oversized_filenames(
    db_session: AsyncSession,
    tmp_data_dir: Path,
) -> None:
    """Filenames > 255 bytes must clamp cleanly to <= 240 UTF-8 bytes without OSError."""
    project = Project(key="longproj", name="LongProj", api_key_hash="k")
    db_session.add(project)
    await db_session.commit()

    settings = Settings(
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        DATA_DIR=str(tmp_data_dir),
        ARCHIVE_RAW_SYMBOLS=True,
    )
    mgr = StorageManager(
        data_dir=tmp_data_dir,
        settings=settings,
        disk_usage_fn=make_disk_usage_fn(free_gb=200.0),
    )

    res = await ingest_symbol(
        session=db_session,
        storage=mgr,
        settings=settings,
        project_id=project.id,
        app_version="1.0.0",
        pdb_filename=("a" * 300) + ".pdb",
        pdb_bytes_or_path=b"PDB_PAYLOAD",
        pe_filename=("b" * 300) + ".exe",
        pe_bytes_or_path=b"PE_PAYLOAD",
        exec_fn=make_mock_exec(returncode=0, stdout=SAMPLE_SYM_WITH_CFI.encode()),
    )
    assert res.symbol.has_pe_unwind is True
    assert res.symbol.raw_archive_path is not None
    raw_name = Path(res.symbol.raw_archive_path).name
    assert raw_name.endswith(".pdb.zst")
    assert len(raw_name.encode("utf-8")) <= 244
