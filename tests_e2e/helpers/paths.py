from __future__ import annotations

import importlib
import os
import socket
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_DIR = REPO_ROOT / "frontend"
CLI_SCRIPT = REPO_ROOT / "cli" / "crashkiller_cli.py"
SEED_SCRIPT = BACKEND_DIR / "scripts" / "seed_demo_data.py"


def require_app_flag() -> bool:
    return os.environ.get("CRASHKILLER_E2E_REQUIRE_APP", "").strip() == "1"


def load_backend_public() -> tuple[Any, Any]:
    """Lazily load only public app entrypoint (create_app) and config (get_settings)."""
    main_file = BACKEND_DIR / "app" / "main.py"
    if not main_file.exists():
        msg = "backend/app/main.py not yet implemented"
        if require_app_flag():
            pytest.fail(msg)
        pytest.skip(msg)

    try:
        main_mod = importlib.import_module("app.main")
        config_mod = importlib.import_module("app.config")
    except ModuleNotFoundError as exc:
        if exc.name not in ("app", "app.main"):
            raise
        main_mod = importlib.import_module("backend.app.main")
        config_mod = importlib.import_module("backend.app.config")

    create_app = getattr(main_mod, "create_app", None)
    if create_app is None:
        app_obj = getattr(main_mod, "app", None)
        if app_obj is None:
            raise AttributeError("app.main must export create_app() or app")

        def _return_app() -> Any:
            return app_obj

        create_app = _return_app

    get_settings = config_mod.get_settings
    return create_app, get_settings


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


_require_app_flag = require_app_flag
_load_backend_public = load_backend_public
_find_free_port = find_free_port
