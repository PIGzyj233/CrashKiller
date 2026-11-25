"""Shared FastAPI integration test harness, mock toolchain, and fake LLM client."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.main import create_app


def _make_disk_usage_controller(initial_free_gb: float = 200.0, total_gb: float = 500.0):
    state = {"free_gb": initial_free_gb, "total_gb": total_gb}
    gib = 1024**3

    def _usage(_path: Path | str) -> SimpleNamespace:
        total = int(state["total_gb"] * gib)
        free = int(state["free_gb"] * gib)
        used = max(0, total - free)
        return SimpleNamespace(total=total, used=used, free=free)

    return state, _usage


def _make_mock_toolchain():
    calls: list[list[str]] = []

    class _FakeProc:
        def __init__(self, rc: int, stdout: bytes, stderr: bytes) -> None:
            self.returncode = rc
            self._stdout = stdout
            self._stderr = stderr

        async def communicate(self) -> tuple[bytes, bytes]:
            return self._stdout, self._stderr

        def kill(self) -> None:
            return None

    async def _exec(*cmd: str, **_kwargs: Any) -> _FakeProc:
        calls.append(list(cmd))
        exe = Path(cmd[0]).name
        if "dump_syms" in exe:
            args = list(cmd[1:])
            has_pe = len(args) >= 2
            pdb_path = Path(args[-1])
            raw_pdb = pdb_path.read_bytes()
            if b"INVALID_PDB" in raw_pdb or not raw_pdb:
                return _FakeProc(1, b"", b"dump_syms: invalid PDB file")
            stem = pdb_path.stem
            did = f"{stem.upper()[:8]:0<8}1111222233334444555566661"
            lines = [
                f"MODULE windows x86_64 {did} {pdb_path.name}",
                f"INFO CODE_ID C0DE{stem.upper()[:4]:0<4} {stem}.dll",
                "FUNC 1000 40 0 App::CrashFunc()",
            ]
            if has_pe:
                lines.append("STACK CFI INIT 1000 40 .cfa: $rsp 8 +")
            return _FakeProc(0, ("\n".join(lines) + "\n").encode("utf-8"), b"")

        if "minidump-stackwalk" in exe:
            dmp_path = next(
                (Path(tok) for tok in cmd[1:] if tok.endswith(".dmp")),
                Path(cmd[2]),
            )
            raw_dmp = dmp_path.read_bytes()
            if b"CORRUPT_DUMP" in raw_dmp or not raw_dmp.startswith(b"MDMP"):
                return _FakeProc(1, b"", b"minidump-stackwalk: corrupt minidump header")

            sym_dirs = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--symbols-path"]
            has_core_sym = any(
                (Path(d) / "Core.pdb").exists() for d in sym_dirs if Path(d).exists()
            )
            payload: dict[str, Any] = {
                "status": "OK",
                "crash_info": {
                    "type": "EXCEPTION_ACCESS_VIOLATION_READ",
                    "address": "0x0000000000000018",
                    "crashing_thread": 0,
                },
                "system_info": {"os": "Windows", "os_ver": "11.0.22631", "cpu_arch": "amd64"},
                "crashing_thread": {
                    "thread_index": 0,
                    "thread_name": "MainThread",
                    "frames": [
                        {
                            "frame": 0,
                            "module": "Core.dll",
                            "module_offset": "0x1234",
                            "function": "Core::RenderFrame()" if has_core_sym else None,
                            "file": "src/core.cpp" if has_core_sym else None,
                            "line": 42 if has_core_sym else None,
                            "trust": "cfi" if has_core_sym else "scan",
                            "missing_symbols": not has_core_sym,
                        },
                        {
                            "frame": 1,
                            "module": "ntdll.dll",
                            "function": "RtlUserThreadStart",
                            "trust": "context",
                        },
                    ],
                    "registers": {"rip": "0x7ff600001234", "rsp": "0x000000a00010"},
                },
                "threads": [],
                "modules": [
                    {
                        "filename": "Core.dll",
                        "debug_file": "Core.pdb",
                        "debug_id": "CORE00001111222233334444555566661",
                        "code_file": "Core.dll",
                        "code_id": "C0DECORE",
                        "loaded_symbols": has_core_sym,
                        "missing_symbols": not has_core_sym,
                    },
                    {
                        "filename": "NoDebugId.dll",
                        "debug_file": "NoDebugId.pdb",
                        "debug_id": None,
                        "loaded_symbols": False,
                        "missing_symbols": True,
                    },
                ],
            }
            return _FakeProc(0, json.dumps(payload).encode("utf-8"), b"")

        return _FakeProc(1, b"", f"Unexpected binary: {exe}".encode())

    return calls, _exec


class _FakeOpenAIClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.queued: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def queue_verdict(
        self,
        verdict: str = "NEW_ISSUE",
        suggested_title: str = "RenderFrame Null Deref",
        matched_issue_id: int | None = None,
    ) -> None:
        self.queued.append(
            {
                "verdict": verdict,
                "matched_issue_id": matched_issue_id,
                "confidence": 0.93,
                "suggested_title": suggested_title,
                "root_cause_analysis": "Null pointer read in Core::RenderFrame.",
                "reasoning_summary": "Verified stack frame and log error lines.",
                "evidence_log_snippets": [],
                "key_log_patterns": ["ERR_RENDER_NULL"],
                "suggested_component": "Rendering",
            }
        )

    async def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        messages = kwargs.get("messages", [])
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if not tool_msgs:
            tc = SimpleNamespace(
                id="call_overview_1",
                type="function",
                function=SimpleNamespace(name="get_crash_overview", arguments="{}"),
            )
            msg = SimpleNamespace(content=None, tool_calls=[tc])
            return SimpleNamespace(
                choices=[SimpleNamespace(message=msg)],
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
            )

        verdict_obj = (
            self.queued.pop(0)
            if self.queued
            else {
                "verdict": "NEW_ISSUE",
                "matched_issue_id": None,
                "confidence": 0.9,
                "suggested_title": "Auto Triaged Issue",
                "root_cause_analysis": "Root cause analysis",
                "reasoning_summary": "Summary",
                "evidence_log_snippets": [],
                "key_log_patterns": [],
                "suggested_component": "Core",
            }
        )
        msg = SimpleNamespace(content=json.dumps(verdict_obj), tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg)],
            usage=SimpleNamespace(prompt_tokens=150, completion_tokens=80),
        )


@asynccontextmanager
async def open_api_harness(tmp_path: Path):
    get_settings.cache_clear()
    db_path = tmp_path / "integration.db"
    data_dir = tmp_path / "data"
    settings = Settings(
        DATABASE_URL=f"sqlite+aiosqlite:///{db_path}",
        REDIS_URL="memory://",
        DATA_DIR=str(data_dir),
        CRASHKILLER_DATA_PATH=str(data_dir),
        ADMIN_USERNAME="fixture-admin",
        ADMIN_PASSWORD="fixture-admin-secret",
        ARCHIVE_RAW_SYMBOLS=True,
        HOST_SAFETY_FLOOR_GB=50.0,
        TOTAL_DISK_QUOTA_GB=150.0,
        LLM_API_KEY="test-key",
        LLM_MODEL="mock-gpt-4o-mini",
        AI_TRIAGE_MODE="SMART_TRIAGE",
        ENABLE_PUBLIC_SYMBOLS=False,
    )
    disk_state, disk_usage_fn = _make_disk_usage_controller(initial_free_gb=200.0)
    calls, exec_fn = _make_mock_toolchain()
    openai_client = _FakeOpenAIClient()

    app = create_app(
        settings=settings,
        disk_usage_fn=disk_usage_fn,
        exec_fn=exec_fn,
        openai_client=openai_client,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            auth=(settings.ADMIN_USERNAME, settings.ADMIN_PASSWORD.get_secret_value()),
        ) as client:
            yield SimpleNamespace(
                app=app,
                client=client,
                settings=settings,
                data_dir=data_dir,
                disk_state=disk_state,
                exec_calls=calls,
                openai_client=openai_client,
            )
    get_settings.cache_clear()


async def _drain(harness: Any) -> None:
    await harness.app.state.dispatcher.drain_and_cancel(timeout=5.0)
