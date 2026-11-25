"""Shared test helpers and fake LLM client for AI triage agent unit tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    CrashLog,
    CrashReport,
    Project,
)
from app.services.clustering import cluster_crash
from app.services.log_engine import LogEngine
from app.services.storage import StorageManager


def _make_response(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    prompt_tokens: int = 100,
    completion_tokens: int = 40,
) -> SimpleNamespace:
    tc_objs = None
    if tool_calls:
        tc_objs = [
            SimpleNamespace(
                id=tc.get("id", f"call_{i}"),
                type="function",
                function=SimpleNamespace(
                    name=tc["name"],
                    arguments=json.dumps(tc.get("arguments", {})),
                ),
            )
            for i, tc in enumerate(tool_calls, start=1)
        ]
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    tool_calls=tc_objs,
                )
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


def _investigate_then_verdict(content: str) -> Any:
    """Return a mock handler that calls get_crash_overview before emitting a verdict."""

    def _handler(_step: int, kwargs: dict[str, Any]) -> Any:
        has_tool_result = any(
            isinstance(m, dict) and m.get("role") == "tool"
            for m in (kwargs.get("messages") or [])
        )
        if not has_tool_result:
            return _make_response(
                tool_calls=[{"id": "ov1", "name": "get_crash_overview", "arguments": {}}]
            )
        return _make_response(content=content)

    return _handler


class FakeOpenAIClient:
    """Scripted mock OpenAI async client capturing outgoing messages."""

    def __init__(self, handler: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self._handler = handler

        async def _create(**kwargs: Any) -> Any:
            self.calls.append(
                {
                    "model": kwargs.get("model"),
                    "messages": json.loads(json.dumps(kwargs.get("messages", []))),
                    "tools": kwargs.get("tools"),
                }
            )
            res = self._handler(len(self.calls), kwargs)
            if asyncio.iscoroutine(res):
                return await res
            return res

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))


async def _seed_crash_with_log(
    session: AsyncSession,
    storage: StorageManager,
    tmp_path: Path,
    *,
    app_version: str = "1.0.0",
    with_log: bool = True,
    log_lines_count: int = 30,
) -> tuple[Project, CrashReport, CrashLog | None]:
    proj = Project(
        key=f"ai-proj-{uuid.uuid4().hex[:6]}",
        name="AI Test Project",
        api_key_hash=hashlib.sha256(b"key").hexdigest(),
        settings_json={},
    )
    session.add(proj)
    await session.flush()

    crash_time = datetime(2026, 11, 22, 10, 0, 5, tzinfo=UTC)
    stacks = {
        "crashing_thread_index": 0,
        "threads": [
            {
                "thread_index": 0,
                "thread_id": 100,
                "thread_name": "MainThread",
                "frames": [
                    {
                        "frame_index": 0,
                        "module": "Renderer.dll",
                        "function": "Renderer::SubmitDraw(int)",
                        "module_offset": "0x1200",
                        "file": "renderer.cpp",
                        "line": 128,
                        "missing_symbols": False,
                        "inlines": [],
                    }
                ],
            },
            {
                "thread_index": 1,
                "thread_id": 200,
                "thread_name": "WorkerIOThread",
                "frames": [
                    {
                        "frame_index": 0,
                        "module": "IO.dll",
                        "function": "DiskCache::Flush()",
                        "module_offset": "0x500",
                        "file": "disk_cache.cpp",
                        "line": 44,
                        "missing_symbols": False,
                        "inlines": [],
                    }
                ],
            },
        ],
        "modules": [],
    }

    crash = CrashReport(
        project_id=proj.id,
        status="TRIAGING",
        app_version=app_version,
        crash_reason="EXCEPTION_ACCESS_VIOLATION_READ",
        crash_address="0x0",
        parsed_summary_json={"top_frames": stacks["threads"][0]["frames"]},
        parsed_stacks_json=stacks,
        missing_symbols_json=[],
        crash_time=crash_time,
    )
    session.add(crash)
    await session.flush()

    await cluster_crash(session, crash, proj, triage_mode="ALWAYS")

    log_row: CrashLog | None = None
    if with_log:
        log_id = uuid.uuid4()
        raw_log = tmp_path / f"raw_{log_id.hex}.log"
        lines_bytes: list[bytes] = []
        for idx in range(1, log_lines_count + 1):
            ts_sec = min(59, idx)
            if idx == 5:
                lines_bytes.append(
                    (
                        f"2026-11-22T10:00:{ts_sec:02d}Z ERROR "
                        f"Renderer::SubmitDraw null texture pointer verbose_line_{idx}\n"
                    ).encode()
                )
            elif idx == 6:
                lines_bytes.append(
                    f"2026-11-22T10:00:{ts_sec:02d}Z FATAL 渲染纹理为空 line_{idx}\n".encode("gbk")
                )
            else:
                lines_bytes.append(
                    (
                        f"2026-11-22T10:00:{ts_sec:02d}Z INFO "
                        f"Frame tick verbose_line_{idx} " + ("x" * 20) + "\n"
                    ).encode("utf-8")
                )
        raw_log.write_bytes(b"".join(lines_bytes))

        rel_zst = f"logs/{crash.id}/{log_id}.log.zst"
        rel_idx = f"logs/{crash.id}/{log_id}.idx.json"
        abs_zst = storage.resolve_path(rel_zst)
        abs_idx = storage.resolve_path(rel_idx)
        abs_zst.parent.mkdir(parents=True, exist_ok=True)
        ingest_info = LogEngine().ingest_log(raw_log, abs_zst, abs_idx)

        log_row = CrashLog(
            id=log_id,
            crash_id=crash.id,
            filename="app.log",
            zst_path=rel_zst,
            idx_path=rel_idx,
            raw_size=ingest_info["raw_size"],
            compressed_size=ingest_info["compressed_size"],
            total_lines=ingest_info["total_lines"],
            digest_json=ingest_info["digest_json"],
        )
        session.add(log_row)

    await session.commit()
    return proj, crash, log_row
