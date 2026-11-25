from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.tier1

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Area 8: Settings, Quotas & GC (T1-42 .. T1-46)
# ---------------------------------------------------------------------------


async def test_t1_42_get_system_settings(client: Any) -> None:
    resp = await client.get("/api/v1/settings")
    assert resp.status_code == 200
    data = resp.json()
    expected_keys = {
        "llm_base_url",
        "llm_api_key_set",
        "llm_api_key_masked",
        "llm_model",
        "ai_triage_mode",
        "ai_max_steps",
        "ai_timeout_seconds",
        "ai_tool_output_max_chars",
        "total_disk_quota_gb",
        "host_safety_floor_gb",
        "db_quota_gb",
        "symbols_sym_quota_gb",
        "symbols_public_cache_quota_gb",
        "symbols_raw_quota_gb",
        "logs_quota_gb",
        "dumps_quota_gb",
        "staging_quota_gb",
        "gc_threshold_percent",
        "log_retention_days",
        "dump_retention_days",
        "staging_ttl_hours",
        "archive_raw_symbols",
        "enable_public_symbols",
        "public_symbol_urls",
        "default_noise_modules",
        "default_noise_functions",
    }
    assert expected_keys.issubset(set(data.keys()))
    assert "llm_api_key" not in data


async def test_t1_43_update_system_settings(client: Any) -> None:
    resp = await client.put(
        "/api/v1/settings",
        json={
            "llm_model": "gpt-4o",
            "ai_max_steps": 6,
            "total_disk_quota_gb": 100.0,
            "host_safety_floor_gb": 40.0,
            "log_retention_days": 15,
        },
    )
    assert resp.status_code == 200
    put_body = resp.json()
    assert put_body["llm_model"] == "gpt-4o"
    assert put_body["ai_max_steps"] == 6
    assert put_body["total_disk_quota_gb"] == 100.0
    assert put_body["host_safety_floor_gb"] == 40.0
    assert put_body["log_retention_days"] == 15

    get_body = (await client.get("/api/v1/settings")).json()
    assert get_body["llm_model"] == "gpt-4o"
    assert get_body["total_disk_quota_gb"] == 100.0


async def test_t1_44_manual_gc_execution(client: Any) -> None:
    resp = await client.post("/api/v1/settings/gc", json={"force_emergency": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["freed_bytes"] >= 0
    assert body["deleted_files_count"] >= 0
    assert body["skipped_pinned_count"] >= 0
    assert body["emergency_triggered"] is False
    assert "partitions" in body


async def test_t1_45_emergency_gc_evicts_unpinned_artifacts(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="gc-evict")
    log_bytes = helpers.make_mixed_encoding_log(num_lines=80)
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(extra_bytes=b"Z" * 4096),
        log_files=[("gc.log", log_bytes)],
    )
    crash_id = up.json()["crash_id"]
    await helpers.wait_for_crash_status(client, crash_id)

    gc_resp = await client.post("/api/v1/settings/gc", json={"force_emergency": True})
    assert gc_resp.status_code == 200
    report = gc_resp.json()
    assert report["emergency_triggered"] is True
    assert report["deleted_files_count"] >= 1
    assert report["freed_bytes"] > 0


async def test_t1_46_dynamic_llm_api_key_masking(client: Any) -> None:
    secret = "sk-test-secret-9876"
    put_resp = await client.put("/api/v1/settings", json={"llm_api_key": secret})
    assert put_resp.status_code == 200

    get_resp = await client.get("/api/v1/settings")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["llm_api_key_set"] is True
    assert secret not in str(data["llm_api_key_masked"])
    assert data["llm_api_key_masked"]


# ---------------------------------------------------------------------------
# Area 9: Dashboard (T1-47 .. T1-51)
# ---------------------------------------------------------------------------


async def test_t1_47_global_dashboard_kpis(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="dash-kpi")
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE")
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump()
    )
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    resp = await client.get("/api/v1/dashboard", params={"days": 14})
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "kpis",
        "crash_trend",
        "verdict_distribution",
        "issue_status_distribution",
        "top_missing_symbols",
        "storage",
    ):
        assert key in body
    assert body["kpis"]["total_crashes"] >= 1
    assert body["kpis"]["analyzed_crashes"] >= 1


async def test_t1_48_project_scoped_dashboard_filtering(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    p_a = await helpers.create_project(client, key="dash-a")
    p_b = await helpers.create_project(client, key="dash-b")

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE")
    u_a = await helpers.upload_crash(client, p_a["key"], p_a["api_key"], fake_toolchain.make_dump())
    await helpers.wait_for_crash_status(client, u_a.json()["crash_id"])

    for _ in range(2):
        mock_llm_server.queue_verdict(verdict="NEW_ISSUE")
        u_b = await helpers.upload_crash(
            client, p_b["key"], p_b["api_key"], fake_toolchain.make_dump()
        )
        await helpers.wait_for_crash_status(client, u_b.json()["crash_id"])

    dash_a = (
        await client.get("/api/v1/dashboard", params={"project_id": p_a["id"], "days": 7})
    ).json()
    assert dash_a["kpis"]["total_crashes"] == 1


async def test_t1_49_dashboard_crash_trend_windows(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="dash-trend")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump()
    )
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    d7 = (await client.get("/api/v1/dashboard", params={"days": 7})).json()
    d30 = (await client.get("/api/v1/dashboard", params={"days": 30})).json()
    assert isinstance(d7["crash_trend"], list) and len(d7["crash_trend"]) >= 1
    assert isinstance(d30["crash_trend"], list) and len(d30["crash_trend"]) >= len(
        d7["crash_trend"]
    )


async def test_t1_50_dashboard_verdict_and_issue_distributions(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="dash-dist")
    known = (
        await client.post(
            "/api/v1/issues",
            json={"project_id": proj["id"], "title": "Fixed One", "status": "FIXED"},
        )
    ).json()

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="New One")
    c1 = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump()
    )
    await helpers.wait_for_crash_status(client, c1.json()["crash_id"])

    mock_llm_server.queue_verdict(verdict="KNOWN_ISSUE", matched_issue_id=known["id"])
    d2 = fake_toolchain.make_dump(
        threads=[{"thread_name": "M", "frames": [{"module": "App.exe", "function": "Other()"}]}]
    )
    c2 = await helpers.upload_crash(client, proj["key"], proj["api_key"], d2)
    await helpers.wait_for_crash_status(client, c2.json()["crash_id"])

    dash = (
        await client.get("/api/v1/dashboard", params={"project_id": proj["id"], "days": 14})
    ).json()
    v_str = str(dash["verdict_distribution"])
    i_str = str(dash["issue_status_distribution"])
    assert "NEW_ISSUE" in v_str and "KNOWN_ISSUE" in v_str
    assert "NEW" in i_str and "FIXED" in i_str


async def test_t1_51_dashboard_storage_watermark_and_top_missing(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="dash-store")
    dmp = fake_toolchain.make_dump(
        modules=[
            {
                "filename": "NoSym.dll",
                "debug_file": "NoSym.pdb",
                "debug_id": "123400000000000000000000000000011",
                "always_loaded": False,
            }
        ],
        threads=[{"thread_name": "M", "frames": [{"module": "NoSym.dll", "function": "F()"}]}],
    )
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    dash = (await client.get("/api/v1/dashboard", params={"project_id": proj["id"]})).json()
    storage = dash["storage"]
    assert isinstance(storage["safety_floor_breached"], bool)
    assert len(dash["top_missing_symbols"]) >= 1
    assert dash["top_missing_symbols"][0]["debug_file"] == "NoSym.pdb"


# ---------------------------------------------------------------------------
# Area 10: CLI & Repo / Frontend / Infra Contracts (T1-52 .. T1-57)
# ---------------------------------------------------------------------------


def test_t1_52_cli_upload_symbols_paired_and_recursive(
    live_server: str,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    import httpx

    proj = httpx.post(
        f"{live_server}/api/v1/projects",
        json={"key": "cli-sym", "name": "CLI Sym"},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()

    build_dir = tmp_path / "build_out"
    sub_dir = build_dir / "nested"
    fake_toolchain.make_pe(
        filename="App.exe",
        code_id="C0DE0001",
        debug_file="App.pdb",
        debug_id="AAA000000000000000000000000000011",
        write_to=build_dir / "App.exe",
    )
    fake_toolchain.make_pdb(
        filename="App.pdb",
        debug_id="AAA000000000000000000000000000011",
        code_file="App.exe",
        code_id="C0DE0001",
        write_to=build_dir / "App.pdb",
    )
    fake_toolchain.make_pdb(
        filename="Lib.pdb",
        debug_id="BBB000000000000000000000000000011",
        code_file="Lib.dll",
        code_id="C0DE0002",
        write_to=sub_dir / "Lib.pdb",
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
            str(build_dir),
            "--app-version",
            "1.0.0",
        ],
        env=isolated_env["proc_env"],
    )
    assert res.returncode == 0, f"CLI stderr: {res.stderr}\nstdout: {res.stdout}"

    syms = httpx.get(
        f"{live_server}/api/v1/symbols",
        params={"project_id": proj["id"]},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()
    assert syms["total"] == 2
    by_file = {s["debug_file"]: s for s in syms["items"]}
    assert by_file["App.pdb"]["has_pe_unwind"] is True
    assert by_file["Lib.pdb"]["has_pe_unwind"] is False


def test_t1_53_cli_upload_crash_wait(
    live_server: str,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    import httpx

    proj = httpx.post(
        f"{live_server}/api/v1/projects",
        json={"key": "cli-crash", "name": "CLI Crash"},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()
    dmp_path = fake_toolchain.fixtures_dir / "cli_test.dmp"
    fake_toolchain.make_dump(write_to=dmp_path)
    log_path = tmp_path / "cli_test.log"
    log_path.write_bytes(helpers.make_mixed_encoding_log(num_lines=20))
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="CLI Wait Crash")

    res = helpers.run_cli(
        [
            "upload-crash",
            "--server",
            live_server,
            "--project",
            proj["key"],
            "--api-key",
            proj["api_key"],
            "--dump",
            str(dmp_path),
            "--log",
            str(log_path),
            "--app-version",
            "1.0.0",
            "--wait",
        ],
        env=isolated_env["proc_env"],
    )
    assert res.returncode == 0, f"CLI stderr: {res.stderr}\nstdout: {res.stdout}"
    combined = res.stdout + res.stderr
    assert "ANALYZED" in combined
    assert "NEW_ISSUE" in combined


def test_t1_54_cli_upload_symbols_dry_run(
    live_server: str,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    import httpx

    proj = httpx.post(
        f"{live_server}/api/v1/projects",
        json={"key": "cli-dry", "name": "CLI Dry"},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()
    build_dir = tmp_path / "dry_build"
    fake_toolchain.make_pdb(write_to=build_dir / "Dry.pdb")

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
            str(build_dir),
            "--app-version",
            "1.0.0",
            "--dry-run",
        ],
        env=isolated_env["proc_env"],
    )
    assert res.returncode == 0, f"CLI stderr: {res.stderr}\nstdout: {res.stdout}"
    syms = httpx.get(
        f"{live_server}/api/v1/symbols",
        params={"project_id": proj["id"]},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()
    assert syms["total"] == 0


def test_t1_55_repo_env_docker_nginx_and_alembic_alignment() -> None:
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    required_env_keys = [
        "APP_NAME",
        "APP_VERSION",
        "DEBUG",
        "API_V1_PREFIX",
        "CORS_ORIGINS",
        "HTTP_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "DATABASE_URL",
        "REDIS_URL",
        "CRASHKILLER_DATA_PATH",
        "DATA_DIR",
        "TOTAL_DISK_QUOTA_GB",
        "HOST_SAFETY_FLOOR_GB",
        "DB_QUOTA_GB",
        "SYMBOLS_SYM_QUOTA_GB",
        "SYMBOLS_PUBLIC_CACHE_QUOTA_GB",
        "SYMBOLS_RAW_QUOTA_GB",
        "LOGS_QUOTA_GB",
        "DUMPS_QUOTA_GB",
        "STAGING_QUOTA_GB",
        "GC_THRESHOLD_PERCENT",
        "LOG_RETENTION_DAYS",
        "DUMP_RETENTION_DAYS",
        "STAGING_TTL_HOURS",
        "ARCHIVE_RAW_SYMBOLS",
        "DUMP_SYMS_BIN",
        "MINIDUMP_STACKWALK_BIN",
        "ENABLE_PUBLIC_SYMBOLS",
        "PUBLIC_SYMBOL_URLS",
        "TOOL_TIMEOUT_SECONDS",
        "LOG_CHUNK_SIZE_BYTES",
        "LOG_ZSTD_LEVEL",
        "DUMP_ZSTD_LEVEL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "AI_TRIAGE_MODE",
        "AI_MAX_STEPS",
        "AI_TIMEOUT_SECONDS",
        "AI_TOOL_OUTPUT_MAX_CHARS",
    ]
    for key in required_env_keys:
        assert f"{key}=" in env_example, f"Missing {key} in .env.example"

    compose_file = REPO_ROOT / "docker-compose.yml"
    nginx_file = REPO_ROOT / "docker" / "nginx.conf"
    alembic_file = REPO_ROOT / "backend" / "alembic" / "versions" / "0001_initial_schema.py"
    backend_dockerfile = REPO_ROOT / "docker" / "backend.Dockerfile"
    frontend_dockerfile = REPO_ROOT / "docker" / "frontend.Dockerfile"
    pyproject_file = REPO_ROOT / "backend" / "pyproject.toml"
    config_file = REPO_ROOT / "backend" / "app" / "config.py"
    require_app = os.environ.get("CRASHKILLER_E2E_REQUIRE_APP", "").strip() == "1"

    core_infra = (compose_file, nginx_file, alembic_file)
    missing = [str(p) for p in core_infra if not p.exists()]
    if missing:
        msg = f"Infra files not yet implemented: {', '.join(missing)}"
        if require_app:
            pytest.fail(msg)
        pytest.skip(msg)

    compose_txt = compose_file.read_text(encoding="utf-8")
    for svc in ("nginx", "api", "worker", "postgres", "redis"):
        assert svc in compose_txt, f"Missing service {svc} in docker-compose.yml"

    mem_limits_mb: list[float] = []
    for m in re.finditer(r"mem_limit:\s*([0-9]+(?:\.[0-9]+)?)\s*([mgkMGK])?", compose_txt):
        val = float(m.group(1))
        unit = (m.group(2) or "b").lower()
        if unit == "g":
            mem_limits_mb.append(val * 1024.0)
        elif unit == "m":
            mem_limits_mb.append(val)
        elif unit == "k":
            mem_limits_mb.append(val / 1024.0)
    assert len(mem_limits_mb) >= 5, (
        f"Expected mem_limit for all services in docker-compose.yml, got {mem_limits_mb}"
    )
    assert sum(mem_limits_mb) <= 12288.0, (
        f"Total compose mem_limit {sum(mem_limits_mb)}MB exceeds 12GB (12288MB)"
    )

    nginx_txt = nginx_file.read_text(encoding="utf-8")
    assert "2048m" in nginx_txt or "2g" in nginx_txt.lower()
    assert "proxy_buffering off" in nginx_txt
    assert "proxy_request_buffering off" in nginx_txt

    alembic_txt = alembic_file.read_text(encoding="utf-8")
    assert "pg_trgm" in alembic_txt

    if backend_dockerfile.exists() or require_app:
        assert backend_dockerfile.is_file(), f"Missing {backend_dockerfile}"
        bdf_txt = backend_dockerfile.read_text(encoding="utf-8")
        assert "dump_syms" in bdf_txt
        assert "minidump-stackwalk" in bdf_txt

    if frontend_dockerfile.exists() or require_app:
        assert frontend_dockerfile.is_file(), f"Missing {frontend_dockerfile}"
        fdf_txt = frontend_dockerfile.read_text(encoding="utf-8")
        assert "npm run build" in fdf_txt and "nginx" in fdf_txt.lower()

    if pyproject_file.exists() or require_app:
        assert pyproject_file.is_file(), f"Missing {pyproject_file}"
        pp_txt = pyproject_file.read_text(encoding="utf-8")
        assert 'requires-python = ">=3.12"' in pp_txt
        for dep in ("fastapi", "sqlalchemy", "zstandard", "arq"):
            assert dep in pp_txt, f"Missing dependency {dep} in backend/pyproject.toml"

    if config_file.exists() or require_app:
        assert config_file.is_file(), f"Missing {config_file}"
        cfg_tree = ast.parse(config_file.read_text(encoding="utf-8"))
        settings_fields: set[str] = set()
        for node in cfg_tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "Settings":
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        settings_fields.add(stmt.target.id)
        for key in required_env_keys:
            assert key in settings_fields, f"Missing Settings field {key} in backend/app/config.py"


@pytest.mark.frontend
@pytest.mark.slow
def test_t1_56_frontend_build_and_deliverables_presence() -> None:
    pkg_json = REPO_ROOT / "frontend" / "package.json"
    require_app = os.environ.get("CRASHKILLER_E2E_REQUIRE_APP", "").strip() == "1"
    if not pkg_json.exists():
        msg = "frontend/package.json not yet implemented"
        if require_app:
            pytest.fail(msg)
        pytest.skip(msg)

    pkg_text = pkg_json.read_text(encoding="utf-8")
    assert "typecheck" in pkg_text and "build" in pkg_text

    required_pages = [
        "DashboardPage.tsx",
        "IssuesPage.tsx",
        "IssueDetailPage.tsx",
        "CrashesPage.tsx",
        "CrashDetailPage.tsx",
        "SymbolsPage.tsx",
        "SettingsPage.tsx",
    ]
    for page in required_pages:
        assert (REPO_ROOT / "frontend" / "src" / "pages" / page).is_file(), f"Missing page {page}"

    required_components = [
        "StackTraceViewer.tsx",
        "VirtualLogViewer.tsx",
        "AgentTracePanel.tsx",
        "UploadModal.tsx",
    ]
    for comp in required_components:
        assert (REPO_ROOT / "frontend" / "src" / "components" / comp).is_file(), (
            f"Missing component {comp}"
        )

    tc = subprocess.run(
        ["npm", "run", "typecheck"],
        cwd=str(REPO_ROOT / "frontend"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert tc.returncode == 0, f"npm run typecheck failed:\n{tc.stdout}\n{tc.stderr}"

    bd = subprocess.run(
        ["npm", "run", "build"],
        cwd=str(REPO_ROOT / "frontend"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert bd.returncode == 0, f"npm run build failed:\n{bd.stdout}\n{bd.stderr}"

    if require_app or (REPO_ROOT / "backend" / "app" / "main.py").exists():
        for extra in (
            REPO_ROOT / "cli" / "cpp_uploader_example" / "CMakeLists.txt",
            REPO_ROOT / "cli" / "cpp_uploader_example" / "main.cpp",
            REPO_ROOT / "cli" / "cpp_uploader_example" / "README.md",
            REPO_ROOT / "backend" / "scripts" / "seed_demo_data.py",
            REPO_ROOT / "README.md",
        ):
            assert extra.is_file(), f"Missing deliverable {extra}"


async def test_t1_57_seed_demo_data_idempotency(
    client: Any,
    isolated_env: dict[str, Any],
) -> None:
    seed_script = REPO_ROOT / "backend" / "scripts" / "seed_demo_data.py"
    require_app = os.environ.get("CRASHKILLER_E2E_REQUIRE_APP", "").strip() == "1"
    if not seed_script.exists():
        msg = "backend/scripts/seed_demo_data.py not yet implemented"
        if require_app:
            pytest.fail(msg)
        pytest.skip(msg)

    import sys

    snapshots: list[tuple[int, int, int]] = []
    for run_no in (1, 2):
        proc = subprocess.run(
            [sys.executable, str(seed_script)],
            env=isolated_env["proc_env"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, (
            f"seed_demo_data run {run_no} failed:\n{proc.stdout}\n{proc.stderr}"
        )

        projs = (await client.get("/api/v1/projects")).json()
        demo_projs = [p for p in projs if p["key"] == "demo-game"]
        assert len(demo_projs) == 1
        pid = demo_projs[0]["id"]

        dash = (await client.get("/api/v1/dashboard", params={"project_id": pid})).json()
        issues = (await client.get("/api/v1/issues", params={"project_id": pid})).json()
        crashes = (await client.get("/api/v1/crashes", params={"project_id": pid})).json()
        assert dash["kpis"]["total_crashes"] >= 1
        assert issues["total"] >= 1
        assert crashes["total"] == dash["kpis"]["total_crashes"]
        snapshots.append((len(demo_projs), issues["total"], crashes["total"]))

    assert snapshots[1] == snapshots[0], (
        f"seed_demo_data.py is not idempotent: run1={snapshots[0]} run2={snapshots[1]}"
    )
