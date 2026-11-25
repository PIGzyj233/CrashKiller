from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

_TESTS_E2E_DIR = str(Path(__file__).resolve().parent)
if _TESTS_E2E_DIR not in sys.path:
    sys.path.insert(0, _TESTS_E2E_DIR)

from helpers import (  # noqa: E402
    BACKEND_DIR,
    CLI_SCRIPT,
    DISK_USAGE_PROXY,
    FAKE_DUMP_SYMS_SCRIPT,
    FAKE_MINIDUMP_STACKWALK_SCRIPT,
    FRONTEND_DIR,
    REPO_ROOT,
    SEED_SCRIPT,
    E2EHelpers,
    FakeDiskUsage,
    FakeToolchain,
    MockLLMServer,
    find_free_port,
    load_backend_public,
    require_app_flag,
)

# Preserve private symbol aliases for tests that import directly from conftest.
_DISK_USAGE_PROXY = DISK_USAGE_PROXY
_FAKE_DUMP_SYMS_SCRIPT = FAKE_DUMP_SYMS_SCRIPT
_FAKE_MINIDUMP_STACKWALK_SCRIPT = FAKE_MINIDUMP_STACKWALK_SCRIPT
_find_free_port = find_free_port
_load_backend_public = load_backend_public
_require_app_flag = require_app_flag

__all__ = [
    "BACKEND_DIR",
    "CLI_SCRIPT",
    "E2EHelpers",
    "FRONTEND_DIR",
    "FakeDiskUsage",
    "FakeToolchain",
    "MockLLMServer",
    "REPO_ROOT",
    "SEED_SCRIPT",
    "_DISK_USAGE_PROXY",
    "_FAKE_DUMP_SYMS_SCRIPT",
    "_FAKE_MINIDUMP_STACKWALK_SCRIPT",
    "_find_free_port",
    "_load_backend_public",
    "_require_app_flag",
]

_MARKERS = (
    "tier1: Tier 1 core feature verification tests",
    "tier2: Tier 2 boundary, quota, and error-path tests",
    "tier3: Tier 3 pairwise feature interaction tests",
    "tier4: Tier 4 full workflow and workload stress tests",
    "tier5: Tier 5 adversarial and security hardening tests",
    "slow: Long-running workload or frontend build tests",
    "frontend: Frontend TypeScript check and Vite production build tests",
)


def pytest_configure(config: Any) -> None:
    for marker in _MARKERS:
        config.addinivalue_line("markers", marker)
    for path_str in (str(REPO_ROOT), str(BACKEND_DIR)):
        if path_str not in sys.path:
            sys.path.insert(0, path_str)
    # Install singleton disk_usage proxy before any backend module binds shutil.disk_usage
    shutil.disk_usage = _DISK_USAGE_PROXY  # type: ignore[assignment]


@pytest.fixture
def fake_toolchain(tmp_path: Path) -> FakeToolchain:
    return FakeToolchain(
        bin_dir=tmp_path / "bin",
        fixtures_dir=tmp_path / "fixtures",
        log_path=tmp_path / "shim_invocations.jsonl",
    )


@pytest.fixture
def mock_llm_server() -> MockLLMServer:
    srv = MockLLMServer()
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture(scope="session", autouse=True)
def _install_disk_usage_proxy() -> Any:
    orig = shutil.disk_usage
    shutil.disk_usage = _DISK_USAGE_PROXY  # type: ignore[assignment]
    try:
        yield _DISK_USAGE_PROXY
    finally:
        shutil.disk_usage = orig


@pytest.fixture
def fake_disk_usage(monkeypatch: pytest.MonkeyPatch) -> Any:
    _DISK_USAGE_PROXY.set_free_gb(200.0, 500.0)
    monkeypatch.setattr(shutil, "disk_usage", _DISK_USAGE_PROXY)
    try:
        yield _DISK_USAGE_PROXY
    finally:
        _DISK_USAGE_PROXY.set_free_gb(200.0, 500.0)


@pytest.fixture
def isolated_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_toolchain: FakeToolchain,
    mock_llm_server: MockLLMServer,
    fake_disk_usage: FakeDiskUsage,
) -> dict[str, Any]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "crashkiller_e2e.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"

    env_vars = {
        "DATABASE_URL": db_url,
        "REDIS_URL": "memory://",
        "DATA_DIR": str(data_dir),
        "CRASHKILLER_DATA_PATH": str(data_dir),
        "ADMIN_USERNAME": "e2e-admin",
        "ADMIN_PASSWORD": "e2e-admin-secret",
        "DUMP_SYMS_BIN": str(fake_toolchain.dump_syms_bin),
        "MINIDUMP_STACKWALK_BIN": str(fake_toolchain.minidump_stackwalk_bin),
        "ENABLE_PUBLIC_SYMBOLS": "false",
        "ARCHIVE_RAW_SYMBOLS": "false",
        "LLM_BASE_URL": mock_llm_server.base_url,
        "LLM_API_KEY": "e2e-test-key",
        "LLM_MODEL": "mock-gpt-4o-mini",
        "AI_TRIAGE_MODE": "SMART_TRIAGE",
        "AI_MAX_STEPS": "10",
        "AI_TIMEOUT_SECONDS": "30",
        "AI_TOOL_OUTPUT_MAX_CHARS": "8000",
        "HOST_SAFETY_FLOOR_GB": "50.0",
        "TOTAL_DISK_QUOTA_GB": "150.0",
        "E2E_SHIM_LOG_PATH": str(fake_toolchain.log_path),
    }
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)

    proc_env = os.environ.copy()
    proc_env.update(env_vars)
    proc_env["PYTHONPATH"] = f"{BACKEND_DIR}{os.pathsep}{REPO_ROOT}"

    return {
        "tmp_path": tmp_path,
        "data_dir": data_dir,
        "db_path": db_path,
        "db_url": db_url,
        "env_vars": env_vars,
        "proc_env": proc_env,
        "admin_auth": (env_vars["ADMIN_USERNAME"], env_vars["ADMIN_PASSWORD"]),
    }


@pytest.fixture
async def app_instance(isolated_env: dict[str, Any]) -> Any:
    create_app, get_settings = _load_backend_public()
    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()

    app = create_app()
    async with app.router.lifespan_context(app):
        yield app

    if hasattr(app, "dependency_overrides"):
        app.dependency_overrides.clear()
    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()


@pytest.fixture
async def client(app_instance: Any, isolated_env: dict[str, Any]) -> Any:
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_instance),
        base_url="http://testserver",
        timeout=30.0,
        auth=isolated_env["admin_auth"],
    ) as ac:
        yield ac


@pytest.fixture
def live_server(isolated_env: dict[str, Any]) -> str:
    create_app, get_settings = _load_backend_public()
    import httpx
    import uvicorn

    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()

    port = _find_free_port()
    app = create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    ready = threading.Event()
    err_holder: list[BaseException] = []

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            ready.set()
            loop.run_until_complete(server.serve())
        except BaseException as exc:  # noqa: BLE001
            err_holder.append(exc)
        finally:
            loop.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    ready.wait(timeout=5.0)

    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15.0
        healthy = False
        while time.monotonic() < deadline:
            if err_holder:
                raise err_holder[0]
            try:
                resp = httpx.get(f"{base_url}/health", timeout=1.0)
                if resp.status_code == 200:
                    healthy = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)

        if not healthy:
            raise RuntimeError(f"live_server failed to become healthy on {base_url}")

        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        if hasattr(get_settings, "cache_clear"):
            get_settings.cache_clear()


@pytest.fixture
def helpers() -> E2EHelpers:
    return E2EHelpers()
