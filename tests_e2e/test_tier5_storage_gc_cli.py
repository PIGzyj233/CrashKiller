from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import REPO_ROOT, SEED_SCRIPT, _load_backend_public

pytestmark = pytest.mark.tier5


async def test_t5_10_per_project_retention_days_respected_by_gc(
    client: Any,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    """GC Step 2 TTL must honor Project.settings_json dump/log_retention_days overrides."""
    await client.put(
        "/api/v1/settings",
        json={"dump_retention_days": 14, "log_retention_days": 14},
    )
    p_long = await helpers.create_project(
        client,
        key="t5-ret-long",
        settings_json={
            "ai_triage_mode": "MANUAL",
            "dump_retention_days": 365,
            "log_retention_days": 365,
        },
    )
    p_short = await helpers.create_project(
        client,
        key="t5-ret-short",
        settings_json={
            "ai_triage_mode": "MANUAL",
            "dump_retention_days": 5,
            "log_retention_days": 5,
        },
    )

    dmp = fake_toolchain.make_dump()
    up_long = await helpers.upload_crash(
        client,
        p_long["key"],
        p_long["api_key"],
        dmp,
        log_files=[("long.log", b"2026-11-22 10:00:00.000 INFO long retain\n")],
    )
    up_short = await helpers.upload_crash(
        client,
        p_short["key"],
        p_short["api_key"],
        dmp,
        log_files=[("short.log", b"2026-11-22 10:00:00.000 INFO short retain\n")],
    )
    c_long_id = up_long.json()["crash_id"]
    l_long_id = up_long.json()["log_ids"][0]
    c_short_id = up_short.json()["crash_id"]
    l_short_id = up_short.json()["log_ids"][0]

    await helpers.wait_for_crash_status(client, c_long_id)
    await helpers.wait_for_crash_status(client, c_short_id)

    # Backdate both crashes by 20 days (> 5d short, > 14d global, < 365d long)
    aged_ts = (dt.datetime.now(dt.UTC) - dt.timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S.%f")
    with sqlite3.connect(isolated_env["db_path"]) as conn:
        conn.execute("UPDATE crash_reports SET created_at = ?", (aged_ts,))
        conn.commit()

    gc = (await client.post("/api/v1/settings/gc", json={"force_emergency": False})).json()
    assert gc["emergency_triggered"] is False
    assert gc["deleted_files_count"] >= 2

    # Long retention project artifacts preserved
    assert (await client.get(f"/api/v1/crashes/{c_long_id}/artifacts/dump")).status_code == 200
    assert (await client.get(f"/api/v1/logs/{l_long_id}/lines")).status_code == 200

    # Short retention project artifacts evicted
    assert (await client.get(f"/api/v1/crashes/{c_short_id}/artifacts/dump")).status_code == 404
    assert (await client.get(f"/api/v1/logs/{l_short_id}/lines")).status_code == 410


async def test_t5_11_gc_symbols_sym_lru_cleans_orphaned_raw_archives(
    client: Any,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    """Step 3e symbol LRU eviction must also unlink orphaned symbols/raw blobs."""
    await client.put(
        "/api/v1/settings",
        json={"archive_raw_symbols": True, "symbols_raw_quota_gb": 15.0},
    )
    proj = await helpers.create_project(client, key="t5-gc-sym-raw")
    did = "5B0011112222333344445555666677771"
    pdb = fake_toolchain.make_pdb(
        filename="RawOrphan.pdb",
        debug_id=did,
        code_file="RawOrphan.dll",
        code_id="6500BBBB",
    )
    pe = fake_toolchain.make_pe(
        filename="RawOrphan.dll",
        code_id="6500BBBB",
        debug_file="RawOrphan.pdb",
        debug_id=did,
    )
    up = await helpers.upload_symbol(
        client,
        proj["key"],
        proj["api_key"],
        pdb,
        pdb_filename="RawOrphan.pdb",
        pe_bytes=pe,
        pe_filename="RawOrphan.dll",
    )
    assert up.status_code == 201
    assert up.json()["raw_size"] > 0

    raw_dir = isolated_env["data_dir"] / "symbols" / "raw"
    sym_dir = isolated_env["data_dir"] / "symbols" / "sym"
    assert len(list(raw_dir.rglob("*.zst"))) >= 1
    assert len(list(sym_dir.rglob("*.sym"))) == 1

    # Shrink symbols_sym_quota_gb so Step 3b skips symbols/raw while Step 3e evicts Symbol
    await client.put(
        "/api/v1/settings",
        json={"symbols_sym_quota_gb": 1e-9, "gc_threshold_percent": 50.0},
    )
    gc = (await client.post("/api/v1/settings/gc", json={"force_emergency": False})).json()
    assert gc["deleted_files_count"] >= 2

    sym_list = (await client.get("/api/v1/symbols", params={"project_id": proj["id"]})).json()
    assert sym_list["total"] == 0
    assert list(sym_dir.rglob("*.sym")) == []
    assert list(raw_dir.rglob("*.zst")) == []


def test_t5_17_cli_multiarch_symbol_dir_no_cross_arch_pe_pairing(
    live_server: str,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    """CLI upload-symbols --dir must not pair x86 PDB with x64 PE from another directory."""
    proj = httpx.post(
        f"{live_server}/api/v1/projects",
        json={"key": "t5-cli-arch", "name": "CLI MultiArch"},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()

    did_x86 = "5C0000000000000000000000000000861"
    did_x64 = "5C0000000000000000000000000000641"
    root = tmp_path / "multiarch"
    fake_toolchain.make_pdb(
        filename="game.pdb",
        debug_id=did_x86,
        code_file="game.dll",
        code_id="C0DE0086",
        write_to=root / "x86" / "game.pdb",
    )
    fake_toolchain.make_pdb(
        filename="game.pdb",
        debug_id=did_x64,
        code_file="game.dll",
        code_id="C0DE0064",
        write_to=root / "x64" / "game.pdb",
    )
    fake_toolchain.make_pe(
        filename="game.dll",
        code_id="C0DE0064",
        debug_file="game.pdb",
        debug_id=did_x64,
        write_to=root / "x64" / "game.dll",
    )

    res = helpers.run_cli(
        [
            "upload-symbols",
            "--server",
            live_server,
            "--project",
            proj["key"],
            "--api-key",
            proj["api_key"],
            "--dir",
            str(root),
            "--app-version",
            "1.0.0",
        ],
        env=isolated_env["proc_env"],
    )
    assert res.returncode == 0, res.stderr

    items = httpx.get(
        f"{live_server}/api/v1/symbols",
        params={"project_id": proj["id"]},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()["items"]
    by_did = {item["debug_id"]: item for item in items}
    assert by_did[did_x86]["has_pe_unwind"] is False
    assert by_did[did_x64]["has_pe_unwind"] is True


async def test_t5_18_seed_script_and_cpp_uploader_strict_compile(
    isolated_env: dict[str, Any],
    tmp_path: Path,
) -> None:
    """seed_demo_data.py runs idempotently twice; C++17 uploader compiles strictly."""
    for run_num in (1, 2):
        proc = subprocess.run(
            [sys.executable, str(SEED_SCRIPT)],
            env=isolated_env["proc_env"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, f"Seed run {run_num} failed: {proc.stderr}\n{proc.stdout}"

    create_app, get_settings = _load_backend_public()
    get_settings.cache_clear()
    app = create_app()
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                timeout=30.0,
                auth=isolated_env["admin_auth"],
            ) as ac:
                dash = (await ac.get("/api/v1/dashboard")).json()
                assert dash["kpis"]["total_crashes"] == 4
                assert dash["kpis"]["total_symbols"] == 3

                crashes = (await ac.get("/api/v1/crashes")).json()["items"]
                assert len(crashes) == 4
                for c in crashes:
                    detail = (await ac.get(f"/api/v1/crashes/{c['id']}")).json()
                    for run in detail["ai_runs"]:
                        for snip in (run.get("result_json") or {}).get("evidence_log_snippets", []):
                            lines_resp = await ac.get(
                                f"/api/v1/logs/{snip['log_id']}/lines",
                                params={"start": snip["start_line"], "end": snip["end_line"]},
                            )
                            assert lines_resp.status_code == 200
                            assert len(lines_resp.json()["lines"]) >= 1
    finally:
        get_settings.cache_clear()

    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is not None:
        cpp_src = REPO_ROOT / "cli" / "cpp_uploader_example" / "main.cpp"
        cpp_bin = tmp_path / "cpp_uploader_check"
        comp = subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-Wall",
                "-Wextra",
                "-Werror",
                str(cpp_src),
                "-o",
                str(cpp_bin),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert comp.returncode == 0, f"C++ compile failed: {comp.stderr}"
        cli_check = subprocess.run(
            [str(cpp_bin)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert cli_check.returncode == 2


async def test_t5_20_symbol_upload_oversized_filenames_clamped_and_served(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    """Uploading symbols with >255-byte pdb_file/pe_file filenames must clamp cleanly (201)."""
    await client.put("/api/v1/settings", json={"archive_raw_symbols": True})
    proj = await helpers.create_project(client, key="t5-long-sym-name")
    did = "5D0011112222333344445555666677771"
    pdb = fake_toolchain.make_pdb(
        filename="LongName.pdb",
        debug_id=did,
        code_file="LongName.dll",
        code_id="6500DDDD",
    )
    pe = fake_toolchain.make_pe(
        filename="LongName.dll",
        code_id="6500DDDD",
        debug_file="LongName.pdb",
        debug_id=did,
    )

    up = await helpers.upload_symbol(
        client,
        proj["key"],
        proj["api_key"],
        pdb,
        pdb_filename=("a" * 300) + ".pdb",
        pe_bytes=pe,
        pe_filename=("b" * 300) + ".dll",
    )
    assert up.status_code == 201, up.text
    body = up.json()
    assert body["has_pe_unwind"] is True
    assert body["raw_size"] > 0

    srv_pdb = await client.get(
        f"/symsrv/{body['debug_file']}/{body['debug_id']}/{body['debug_file']}"
    )
    srv_pe = await client.get(f"/symsrv/{body['code_file']}/{body['code_id']}/{body['code_file']}")
    assert srv_pdb.status_code == 200
    assert srv_pe.status_code == 200


async def test_t5_21_gc_symbols_raw_lru_respects_cross_project_refcount(
    client: Any,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    """Step 3b symbols/raw GC must evict unshared older raw file and keep shared /symsrv 200."""
    await client.put(
        "/api/v1/settings",
        json={
            "archive_raw_symbols": True,
            "symbols_raw_quota_gb": 15.0,
            "gc_threshold_percent": 50.0,
        },
    )
    p_a = await helpers.create_project(client, key="t5-raw-ref-a")
    p_b = await helpers.create_project(client, key="t5-raw-ref-b")
    p_c = await helpers.create_project(client, key="t5-raw-ref-c")

    did_shared = "5E0011112222333344445555666677771"
    did_old = "5F0011112222333344445555666677771"
    pdb_shared = fake_toolchain.make_pdb(
        filename="SharedMod.pdb",
        debug_id=did_shared,
        code_file="SharedMod.dll",
        code_id="6500EEEE",
    )
    pdb_old = fake_toolchain.make_pdb(
        filename="OldMod.pdb",
        debug_id=did_old,
        code_file="OldMod.dll",
        code_id="6500FFFF",
    )

    s_a = (
        await helpers.upload_symbol(
            client, p_a["key"], p_a["api_key"], pdb_shared, pdb_filename="SharedMod.pdb"
        )
    ).json()
    s_c = (
        await helpers.upload_symbol(
            client, p_c["key"], p_c["api_key"], pdb_old, pdb_filename="OldMod.pdb"
        )
    ).json()
    s_b = (
        await helpers.upload_symbol(
            client, p_b["key"], p_b["api_key"], pdb_shared, pdb_filename="SharedMod.pdb"
        )
    ).json()

    now = dt.datetime.now(dt.UTC)
    ts_a = (now - dt.timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S.%f")
    ts_c = (now - dt.timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S.%f")
    ts_b = now.strftime("%Y-%m-%d %H:%M:%S.%f")
    with sqlite3.connect(isolated_env["db_path"]) as conn:
        for sym_id_hex, ts_val in (
            (s_a["id"].replace("-", ""), ts_a),
            (s_c["id"].replace("-", ""), ts_c),
            (s_b["id"].replace("-", ""), ts_b),
        ):
            conn.execute(
                "UPDATE symbols SET last_used_at = ? WHERE id = ?",
                (ts_val, sym_id_hex),
            )
        conn.commit()

    shared_zst = isolated_env["data_dir"] / "symbols" / "raw" / did_shared / "SharedMod.pdb.zst"
    old_zst = isolated_env["data_dir"] / "symbols" / "raw" / did_old / "OldMod.pdb.zst"
    assert shared_zst.is_file() and old_zst.is_file()
    shared_sz = shared_zst.stat().st_size

    # Set target (50% of quota) to shared_sz + 8 bytes so only 1 file (OldMod) is evicted
    quota_gb = ((shared_sz + 8) * 2) / (1024**3)
    await client.put(
        "/api/v1/settings",
        json={"symbols_raw_quota_gb": quota_gb, "gc_threshold_percent": 50.0},
    )

    gc = (await client.post("/api/v1/settings/gc", json={"force_emergency": False})).json()
    assert gc["deleted_files_count"] >= 1

    assert (await client.get(f"/symsrv/OldMod.pdb/{did_old}/OldMod.pdb")).status_code == 404
    srv_shared = await client.get(f"/symsrv/SharedMod.pdb/{did_shared}/SharedMod.pdb")
    assert srv_shared.status_code == 200


async def test_t5_22_symbol_delete_cleans_all_raw_and_cli_three_dir_no_mispair(
    client: Any,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    """Symbol upgrade+delete cleans all symbols/raw .zst; CLI skips ambiguous 3-dir pairing."""
    await client.put("/api/v1/settings", json={"archive_raw_symbols": True})
    proj = await helpers.create_project(client, key="t5-clean-all-raw")
    did = "5A1111112222333344445555666677771"
    pdb = fake_toolchain.make_pdb(
        filename="CleanMod.pdb",
        debug_id=did,
        code_file="CleanMod.dll",
        code_id="6500AAAA",
    )
    pe = fake_toolchain.make_pe(
        filename="CleanMod.dll",
        code_id="6500AAAA",
        debug_file="CleanMod.pdb",
        debug_id=did,
    )

    up1 = await helpers.upload_symbol(
        client, proj["key"], proj["api_key"], pdb, pdb_filename="CleanMod.pdb"
    )
    assert up1.status_code == 201
    up2 = await helpers.upload_symbol(
        client,
        proj["key"],
        proj["api_key"],
        pdb,
        pdb_filename="CleanMod.pdb",
        pe_bytes=pe,
        pe_filename="CleanMod.dll",
    )
    assert up2.status_code == 201
    sym_id = up2.json()["id"]

    raw_dir = isolated_env["data_dir"] / "symbols" / "raw"
    assert len(list(raw_dir.rglob("*.zst"))) == 2
    assert (await client.delete(f"/api/v1/symbols/{sym_id}")).status_code == 204
    assert list(raw_dir.rglob("*.zst")) == []
    assert (await client.get(f"/symsrv/CleanMod.pdb/{did}/CleanMod.pdb")).status_code == 404
    assert (await client.get("/symsrv/CleanMod.dll/6500AAAA/CleanMod.dll")).status_code == 404

    # CLI 3-dir check: x86/foo.pdb + x64/foo.pdb + shared/foo.dll must NOT mis-pair
    three_dir = tmp_path / "three_dir"
    (three_dir / "x86").mkdir(parents=True)
    (three_dir / "x64").mkdir(parents=True)
    (three_dir / "shared").mkdir(parents=True)
    (three_dir / "x86" / "foo.pdb").write_bytes(b"PDB86")
    (three_dir / "x64" / "foo.pdb").write_bytes(b"PDB64")
    (three_dir / "shared" / "foo.dll").write_bytes(b"DLL")

    dry = helpers.run_cli(
        [
            "upload-symbols",
            "--server",
            "http://127.0.0.1:1",
            "--project",
            "ignored",
            "--api-key",
            "ignored",
            "--dir",
            str(three_dir),
            "--app-version",
            "1.0.0",
            "--dry-run",
        ],
        env=isolated_env["proc_env"],
    )
    assert dry.returncode == 0, dry.stderr
    assert "pe=(none)" in dry.stdout
    assert "foo.dll" not in dry.stdout
