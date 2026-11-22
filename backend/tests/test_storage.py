"""Unit tests for StorageManager, disk quotas, safety floor, and pin-protected GC."""

from __future__ import annotations

import io
import os
import shutil
import uuid
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import AIAnalysisRun, CrashLog, CrashReport, Issue, Project, Symbol
from app.services.storage import GIB, PARTITIONS, StorageError, StorageManager
from tests.conftest import make_disk_usage_fn


def test_storage_layout_and_atomic_write(storage_manager: StorageManager) -> None:
    for part in PARTITIONS:
        assert (storage_manager.data_dir / part).is_dir()

    written = storage_manager.atomic_write_bytes(
        "symbols/sym/test.sym",
        b"MODULE windows x86_64 1 a.pdb\n",
    )
    assert written.read_bytes().startswith(b"MODULE ")

    # No stray .tmp_ files left behind
    sym_dir = storage_manager.partition_path("symbols/sym")
    assert not any(p.name.startswith(".tmp_") for p in sym_dir.rglob("*"))

    with pytest.raises(StorageError):
        storage_manager.resolve_path("../escape.txt")

    with pytest.raises(StorageError):
        storage_manager.resolve_path("/etc/passwd")

    with pytest.raises(StorageError):
        storage_manager.resolve_path("dumps/evil\x00.dmp")

    for bad_rel in ("", ".", "   "):
        with pytest.raises(StorageError):
            storage_manager.resolve_path(bad_rel)
        with pytest.raises(StorageError):
            storage_manager.atomic_write_bytes(bad_rel, b"x")
        with pytest.raises(StorageError):
            storage_manager.atomic_write_stream(bad_rel, io.BytesIO(b"x"))

    with pytest.raises(StorageError):
        storage_manager.atomic_write_bytes("dumps", b"x")

    assert not any(p.name.startswith(".tmp_") for p in storage_manager.data_dir.parent.iterdir())


def test_config_rejects_json_object_for_str_list() -> None:
    with pytest.raises(ValidationError):
        Settings(CORS_ORIGINS='{"a": 1}')  # type: ignore[arg-type]


def test_dump_zstd_roundtrip(storage_manager: StorageManager, tmp_path: Path) -> None:
    src_dump = tmp_path / "minidump.dmp"
    payload = b"MDMP" + (b"ABCD1234" * 4096)
    src_dump.write_bytes(payload)
    crash_id = uuid.uuid4()

    rel_path, raw_size, comp_size = storage_manager.compress_dump(src_dump, crash_id)
    assert rel_path == f"dumps/{crash_id}/{crash_id}.dmp.zst"
    assert raw_size == len(payload)
    assert 0 < comp_size < raw_size

    out_dump = tmp_path / "restored.dmp"
    storage_manager.decompress_dump(rel_path, out_dump)
    assert out_dump.read_bytes() == payload


def test_disk_usage_report_and_host_safety_floor(
    tmp_data_dir: Path,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    breached_mgr = StorageManager(
        data_dir=tmp_data_dir,
        settings=test_settings,
        disk_usage_fn=make_disk_usage_fn(free_gb=40.0),
    )
    assert breached_mgr.is_host_floor_breached() is True
    report = breached_mgr.get_disk_usage_report()
    assert report["safety_floor_breached"] is True
    assert report["host_free_gb"] == pytest.approx(40.0, rel=1e-3)

    raw_src = tmp_path / "app.pdb"
    raw_src.write_bytes(b"PDBDATA")
    assert breached_mgr.compress_raw_symbol(raw_src, "symbols/raw/ID1/app.pdb.zst") is None

    healthy_mgr = StorageManager(
        data_dir=tmp_data_dir,
        settings=test_settings,
        disk_usage_fn=make_disk_usage_fn(free_gb=120.0),
    )
    assert healthy_mgr.is_host_floor_breached() is False
    archived = healthy_mgr.compress_raw_symbol(raw_src, "symbols/raw/ID1/app.pdb.zst")
    assert archived is not None


async def test_gc_protects_pinned_crash_and_pinned_issue(
    db_session: AsyncSession,
    tmp_data_dir: Path,
    test_settings: Settings,
) -> None:
    mgr = StorageManager(
        data_dir=tmp_data_dir,
        settings=test_settings,
        disk_usage_fn=make_disk_usage_fn(free_gb=40.0),  # Triggers safety floor breach
    )

    project = Project(key="game", name="Game", api_key_hash="hash")
    db_session.add(project)
    await db_session.flush()

    pinned_issue = Issue(
        project_id=project.id,
        title="Pinned Known Issue",
        status="KNOWN",
        is_pinned=True,
    )
    unpinned_issue = Issue(
        project_id=project.id,
        title="Unpinned Issue",
        status="NEW",
        is_pinned=False,
    )
    db_session.add_all([pinned_issue, unpinned_issue])
    await db_session.flush()

    old_ts = datetime.now(UTC) - timedelta(days=45)

    # 1. Crash directly pinned (is_pinned=True)
    c1_id = uuid.uuid4()
    c1_dump = f"dumps/{c1_id}/{c1_id}.dmp.zst"
    c1_log_zst = f"logs/{c1_id}/app.log.zst"
    c1_log_idx = f"logs/{c1_id}/app.idx.json"
    mgr.atomic_write_bytes(c1_dump, b"D1" * 1024)
    mgr.atomic_write_bytes(c1_log_zst, b"L1" * 1024)
    mgr.atomic_write_bytes(c1_log_idx, b"{}")

    # 2. Crash NOT directly pinned, but linked to pinned Issue (is_pinned=True)
    c2_id = uuid.uuid4()
    c2_dump = f"dumps/{c2_id}/{c2_id}.dmp.zst"
    c2_log_zst = f"logs/{c2_id}/app.log.zst"
    c2_log_idx = f"logs/{c2_id}/app.idx.json"
    mgr.atomic_write_bytes(c2_dump, b"D2" * 1024)
    mgr.atomic_write_bytes(c2_log_zst, b"L2" * 1024)
    mgr.atomic_write_bytes(c2_log_idx, b"{}")

    # 3. Unpinned expired crash (should be deleted while preserving DB metadata)
    c3_id = uuid.uuid4()
    c3_dump = f"dumps/{c3_id}/{c3_id}.dmp.zst"
    c3_log_zst = f"logs/{c3_id}/app.log.zst"
    c3_log_idx = f"logs/{c3_id}/app.idx.json"
    mgr.atomic_write_bytes(c3_dump, b"D3" * 2048)
    mgr.atomic_write_bytes(c3_log_zst, b"L3" * 2048)
    mgr.atomic_write_bytes(c3_log_idx, b"{}")

    c1 = CrashReport(
        id=c1_id,
        project_id=project.id,
        issue_id=unpinned_issue.id,
        status="ANALYZED",
        app_version="1.0.0",
        dump_path=c1_dump,
        is_pinned=True,
        created_at=old_ts,
        crash_time=old_ts,
    )
    c2 = CrashReport(
        id=c2_id,
        project_id=project.id,
        issue_id=pinned_issue.id,
        status="ANALYZED",
        app_version="1.0.0",
        dump_path=c2_dump,
        is_pinned=False,
        created_at=old_ts,
        crash_time=old_ts,
    )
    c3 = CrashReport(
        id=c3_id,
        project_id=project.id,
        issue_id=unpinned_issue.id,
        status="ANALYZED",
        app_version="1.0.0",
        dump_path=c3_dump,
        parsed_stacks_json={"threads": [{"frames": [{"function": "Main"}]}]},
        ai_verdict="NEW_ISSUE",
        is_pinned=False,
        created_at=old_ts,
        crash_time=old_ts,
    )
    db_session.add_all([c1, c2, c3])
    await db_session.flush()

    l1 = CrashLog(
        crash_id=c1.id,
        filename="app.log",
        zst_path=c1_log_zst,
        idx_path=c1_log_idx,
        total_lines=10,
        digest_json={"total_lines": 10},
    )
    l2 = CrashLog(
        crash_id=c2.id,
        filename="app.log",
        zst_path=c2_log_zst,
        idx_path=c2_log_idx,
        total_lines=20,
        digest_json={"total_lines": 20},
    )
    l3 = CrashLog(
        crash_id=c3.id,
        filename="app.log",
        zst_path=c3_log_zst,
        idx_path=c3_log_idx,
        total_lines=30,
        digest_json={"total_lines": 30},
    )
    ai_run = AIAnalysisRun(
        crash_id=c3.id,
        model_name="gpt-4o-mini",
        status="COMPLETED",
        result_json={"verdict": "NEW_ISSUE"},
    )
    db_session.add_all([l1, l2, l3, ai_run])
    await db_session.commit()

    gc_res = await mgr.run_gc(db_session, force_emergency=True)
    assert gc_res["emergency_triggered"] is True
    assert gc_res["skipped_pinned_count"] >= 4
    assert gc_res["deleted_files_count"] >= 3
    assert gc_res["freed_bytes"] > 0

    # Pinned files remain intact
    assert mgr.resolve_path(c1_dump).exists()
    assert mgr.resolve_path(c1_log_zst).exists()
    assert mgr.resolve_path(c2_dump).exists()
    assert mgr.resolve_path(c2_log_zst).exists()

    # Unpinned files removed, DB metadata preserved
    assert not mgr.resolve_path(c3_dump).exists()
    assert not mgr.resolve_path(c3_log_zst).exists()

    refreshed_c3 = (
        await db_session.execute(select(CrashReport).where(CrashReport.id == c3_id))
    ).scalar_one()
    assert refreshed_c3.dump_path is None
    assert refreshed_c3.parsed_stacks_json == {"threads": [{"frames": [{"function": "Main"}]}]}
    assert refreshed_c3.ai_verdict == "NEW_ISSUE"

    refreshed_l3 = (
        await db_session.execute(select(CrashLog).where(CrashLog.crash_id == c3_id))
    ).scalar_one()
    assert refreshed_l3.zst_path is None
    assert refreshed_l3.idx_path is None
    assert refreshed_l3.digest_json == {"total_lines": 30}


async def test_automatic_gc_stops_at_floor_and_protects_active_work(
    db_session: AsyncSession, tmp_data_dir: Path, test_settings: Settings
) -> None:
    paths: list[Path] = []
    disk_snapshot = namedtuple("DiskUsage", "total used free")

    def disk_usage(_path: Path | str):
        free = 50 * GIB - 1 + sum(16 for path in paths if not path.is_file())
        return disk_snapshot(100 * GIB, 100 * GIB - free, free)

    storage = StorageManager(tmp_data_dir, test_settings, disk_usage_fn=disk_usage)
    project = Project(key="emergency-gc", name="Emergency GC", api_key_hash="test-key")
    db_session.add(project)
    await db_session.flush()
    now = datetime.now(UTC)
    for index, crash_status in enumerate(("PARSING", "ANALYZED", "ANALYZED")):
        crash_id = uuid.uuid4()
        dump_path = f"dumps/{crash_id}/sample.dmp.zst"
        paths.append(storage.atomic_write_bytes(dump_path, b"s" * 16))
        db_session.add(
            CrashReport(
                id=crash_id,
                project_id=project.id,
                app_version="1.0.0",
                status=crash_status,
                created_at=now + timedelta(seconds=index),
                crash_time=now,
                dump_path=dump_path,
            )
        )
    await db_session.commit()

    with storage.staging_workspace("parse") as workspace:
        active_input = workspace / "crash.dmp"
        active_input.write_bytes(b"active")
        report = await storage.run_gc(db_session)
        assert active_input.is_file()

    assert report["emergency_triggered"] is True
    assert report["freed_bytes"] == 16
    assert [path.is_file() for path in paths] == [True, False, True]


async def test_gc_lru_eviction_by_partition_quota(
    db_session: AsyncSession,
    tmp_data_dir: Path,
) -> None:
    # Configure tiny quotas so LRU triggers deterministically
    tiny_gb = 1000.0 / (1024**3)  # ~1000 bytes quota
    settings = Settings(
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        DATA_DIR=str(tmp_data_dir),
        SYMBOLS_RAW_QUOTA_GB=tiny_gb,
        SYMBOLS_PUBLIC_CACHE_QUOTA_GB=tiny_gb,
        GC_THRESHOLD_PERCENT=50.0,
        HOST_SAFETY_FLOOR_GB=1.0,
    )
    mgr = StorageManager(
        data_dir=tmp_data_dir,
        settings=settings,
        disk_usage_fn=make_disk_usage_fn(free_gb=200.0),
    )

    # Staging old file (>6h) vs recent file
    old_stage = mgr.atomic_write_bytes("staging/old.tmp", b"S" * 100)
    old_mtime = (datetime.now(UTC) - timedelta(hours=10)).timestamp()
    os.utime(old_stage, (old_mtime, old_mtime))
    new_stage = mgr.atomic_write_bytes("staging/new.tmp", b"N" * 100)

    # Public cache LRU
    old_pub = mgr.atomic_write_bytes("symbols/public_cache/old.sym", b"P" * 700)
    os.utime(old_pub, (old_mtime, old_mtime))
    new_pub = mgr.atomic_write_bytes("symbols/public_cache/new.sym", b"P" * 400)

    project = Project(key="p2", name="P2", api_key_hash="h2")
    db_session.add(project)
    await db_session.flush()

    now = datetime.now(UTC)
    s_old_raw = "symbols/raw/OLD/old.pdb.zst"
    s_new_raw = "symbols/raw/NEW/new.pdb.zst"
    mgr.atomic_write_bytes(s_old_raw, b"R" * 700)
    mgr.atomic_write_bytes(s_new_raw, b"R" * 400)
    mgr.atomic_write_bytes("symbols/sym/old.pdb/OLD/old.sym", b"SYM1")
    mgr.atomic_write_bytes("symbols/sym/new.pdb/NEW/new.sym", b"SYM2")

    sym_old = Symbol(
        project_id=project.id,
        app_version="1.0",
        module_name="old",
        debug_file="old.pdb",
        debug_id="OLD",
        sym_path="symbols/sym/old.pdb/OLD/old.sym",
        sym_size=4,
        raw_archive_path=s_old_raw,
        raw_size=700,
        last_used_at=now - timedelta(days=5),
    )
    sym_new = Symbol(
        project_id=project.id,
        app_version="1.0",
        module_name="new",
        debug_file="new.pdb",
        debug_id="NEW",
        sym_path="symbols/sym/new.pdb/NEW/new.sym",
        sym_size=4,
        raw_archive_path=s_new_raw,
        raw_size=400,
        last_used_at=now,
    )
    db_session.add_all([sym_old, sym_new])
    await db_session.commit()

    res = await mgr.run_gc(db_session, force_emergency=False)
    assert res["emergency_triggered"] is False

    assert not old_stage.exists()
    assert new_stage.exists()

    assert not old_pub.exists()
    assert new_pub.exists()

    assert not mgr.resolve_path(s_old_raw).exists()
    assert mgr.resolve_path(s_new_raw).exists()
    # .sym must still exist
    assert mgr.resolve_path("symbols/sym/old.pdb/OLD/old.sym").exists()


def test_storage_manager_monkeypatched_shutil_disk_usage(
    tmp_data_dir: Path,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default StorageManager(disk_usage_fn=None) must resolve shutil.disk_usage dynamically."""
    DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
    mgr = StorageManager(data_dir=tmp_data_dir, settings=test_settings)

    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: DiskUsage(total=500 * 1024**3, used=490 * 1024**3, free=10 * 1024**3),
    )
    assert mgr.is_host_floor_breached() is True

    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: DiskUsage(total=500 * 1024**3, used=300 * 1024**3, free=200 * 1024**3),
    )
    assert mgr.is_host_floor_breached() is False
    report = mgr.get_disk_usage_report()
    assert report["safety_floor_breached"] is False


def test_compress_and_decompress_dump_rejects_escape_and_nul_bytes(
    storage_manager: StorageManager,
    tmp_path: Path,
) -> None:
    for bad_crash_id in ("../logs/evil", "a/b", "a\\b", ".", "..", "", "   ", "bad\x00id"):
        with pytest.raises(StorageError):
            storage_manager.compress_dump(io.BytesIO(b"MDMP"), bad_crash_id)

    evil_sym = storage_manager.partition_path("dumps") / "sym_escape"
    evil_sym.symlink_to(storage_manager.partition_path("logs"))
    with pytest.raises(StorageError):
        storage_manager.compress_dump(io.BytesIO(b"MDMP"), "sym_escape")

    with pytest.raises(StorageError, match="NUL byte"):
        storage_manager.compress_dump(str(tmp_path / "in\x00.dmp"), uuid.uuid4())
    with pytest.raises(StorageError, match="NUL byte"):
        storage_manager.compress_dump(tmp_path / "in\x00.dmp", uuid.uuid4())

    rel_path, _, _ = storage_manager.compress_dump(io.BytesIO(b"MDMP_VALID"), uuid.uuid4())
    with pytest.raises(StorageError, match="NUL byte"):
        storage_manager.decompress_dump(rel_path, str(tmp_path / "out\x00.dmp"))
    with pytest.raises(StorageError, match="NUL byte"):
        storage_manager.decompress_dump(rel_path, tmp_path / "out\x00.dmp")

    with pytest.raises(StorageError, match="NUL byte"):
        storage_manager.compress_raw_symbol("foo\x00bar.pdb", "symbols/raw/ID/foo.pdb.zst")


async def test_gc_symbols_raw_lru_respects_cross_project_refcount(
    db_session: AsyncSession,
    tmp_data_dir: Path,
) -> None:
    """Step 3b symbols/raw LRU must refcount shared raw archives & PE archives across projects."""
    quota_500b = 500.0 / (1024**3)
    settings = Settings(
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        DATA_DIR=str(tmp_data_dir),
        SYMBOLS_RAW_QUOTA_GB=quota_500b,
        GC_THRESHOLD_PERCENT=50.0,  # target = 250 bytes
        HOST_SAFETY_FLOOR_GB=1.0,
    )
    mgr = StorageManager(
        data_dir=tmp_data_dir,
        settings=settings,
        disk_usage_fn=make_disk_usage_fn(free_gb=200.0),
    )

    p1 = Project(key="p1", name="P1", api_key_hash="h1")
    p2 = Project(key="p2", name="P2", api_key_hash="h2")
    p3 = Project(key="p3", name="P3", api_key_hash="h3")
    db_session.add_all([p1, p2, p3])
    await db_session.flush()

    shared_pdb = "symbols/raw/SHARED1/Shared.pdb.zst"
    old_pdb = "symbols/raw/OLD2/Old.pdb.zst"
    shared_dll = "symbols/raw/CODE1/Shared.dll.zst"
    mgr.atomic_write_bytes(shared_pdb, b"S" * 200)
    mgr.atomic_write_bytes(old_pdb, b"O" * 200)
    mgr.atomic_write_bytes(shared_dll, b"D" * 200)

    now = datetime.now(UTC)
    s_a = Symbol(
        project_id=p1.id,
        app_version="1.0",
        module_name="Shared",
        debug_file="Shared.pdb",
        debug_id="SHARED1",
        sym_path=f"symbols/sym/{p1.id}/Shared.pdb/SHARED1/Shared.sym",
        sym_size=10,
        raw_archive_path=shared_pdb,
        raw_size=200,
        last_used_at=now - timedelta(days=10),
    )
    s_c = Symbol(
        project_id=p3.id,
        app_version="1.0",
        module_name="Old",
        debug_file="Old.pdb",
        debug_id="OLD2",
        sym_path=f"symbols/sym/{p3.id}/Old.pdb/OLD2/Old.sym",
        sym_size=10,
        raw_archive_path=old_pdb,
        raw_size=200,
        last_used_at=now - timedelta(days=5),
    )
    # P1 also has a PE-only symbol sharing CODE1/Shared.dll.zst with P2's PDB+PE symbol
    s_pe_old = Symbol(
        project_id=p1.id,
        app_version="1.0",
        module_name="SharedDll",
        debug_file="SharedDll.pdb",
        debug_id="DBG_DLL_1",
        code_file="Shared.dll",
        code_id="CODE1",
        has_pe_unwind=True,
        sym_path=f"symbols/sym/{p1.id}/SharedDll.pdb/DBG_DLL_1/SharedDll.sym",
        sym_size=10,
        raw_archive_path=shared_dll,
        raw_size=200,
        last_used_at=now - timedelta(days=8),
    )
    s_b = Symbol(
        project_id=p2.id,
        app_version="1.0",
        module_name="Shared",
        debug_file="Shared.pdb",
        debug_id="SHARED1",
        code_file="Shared.dll",
        code_id="CODE1",
        has_pe_unwind=True,
        sym_path=f"symbols/sym/{p2.id}/Shared.pdb/SHARED1/Shared.sym",
        sym_size=10,
        raw_archive_path=shared_pdb,
        raw_size=400,
        last_used_at=now,
    )
    db_session.add_all([s_a, s_c, s_pe_old, s_b])
    await db_session.commit()

    # Total raw_used = 600 bytes; target = 250 bytes.
    # Visiting s_a (-10d) decrements Shared.pdb refcount 2->1 (kept),
    # visiting s_pe_old (-8d) decrements Shared.dll pe_count 2->1 (kept),
    # visiting s_c (-5d) evicts Old.pdb (600->400), then visiting s_b (now) evicts both
    # wait: to verify s_c is evicted BEFORE s_b while Shared.pdb/Shared.dll survive,
    # test with target = 450 bytes (quota = 900 bytes, 50% = 450 bytes):
    mgr.settings = settings.model_copy(
        update={"SYMBOLS_RAW_QUOTA_GB": 900.0 / (1024**3), "GC_THRESHOLD_PERCENT": 50.0}
    )
    await mgr.run_gc(db_session, force_emergency=False)

    assert not mgr.resolve_path(old_pdb).exists()
    assert mgr.resolve_path(shared_pdb).exists()
    assert mgr.resolve_path(shared_dll).exists()

    await db_session.refresh(s_a)
    await db_session.refresh(s_pe_old)
    await db_session.refresh(s_c)
    await db_session.refresh(s_b)
    assert s_a.raw_archive_path is None and s_a.raw_size == 0
    assert s_pe_old.raw_archive_path is None and s_pe_old.raw_size == 0
    assert s_c.raw_archive_path is None and s_c.raw_size == 0
    assert s_b.raw_archive_path == shared_pdb
