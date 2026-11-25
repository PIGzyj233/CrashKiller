from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from .paths import CLI_SCRIPT, REPO_ROOT, require_app_flag


class E2EHelpers:
    """Opaque-box HTTP, SSE, log generation, and CLI test helpers."""

    async def create_project(
        self,
        client: Any,
        key: str | None = None,
        name: str = "Test Project",
        settings_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        proj_key = key or f"proj-{uuid.uuid4().hex[:8]}"
        body: dict[str, Any] = {"key": proj_key, "name": name}
        if settings_json is not None:
            body["settings_json"] = settings_json
        resp = await client.post("/api/v1/projects", json=body)
        assert resp.status_code == 201, f"create_project failed: {resp.status_code} {resp.text}"
        return resp.json()

    async def upload_symbol(
        self,
        client: Any,
        project_key: str,
        api_key: str | None,
        pdb_bytes: bytes,
        pdb_filename: str = "MyApp.pdb",
        pe_bytes: bytes | None = None,
        pe_filename: str = "MyApp.exe",
        app_version: str = "1.0.0",
        use_header: bool = True,
        project_id: str | None = None,
    ) -> Any:
        data: dict[str, str] = {"app_version": app_version}
        if project_id is not None:
            data["project_id"] = project_id
        else:
            data["project_key"] = project_key
        headers: dict[str, str] = {}
        if api_key is not None:
            if use_header:
                headers["X-API-Key"] = api_key
            else:
                data["api_key"] = api_key

        files: list[tuple[str, tuple[str, bytes, str]]] = [
            ("pdb_file", (pdb_filename, pdb_bytes, "application/octet-stream"))
        ]
        if pe_bytes is not None:
            files.append(("pe_file", (pe_filename, pe_bytes, "application/octet-stream")))

        return await client.post("/api/v1/symbols/upload", data=data, files=files, headers=headers)

    async def upload_crash(
        self,
        client: Any,
        project_key: str,
        api_key: str | None,
        dump_bytes: bytes | None,
        dump_filename: str = "crash.dmp",
        log_files: list[tuple[str, bytes]] | None = None,
        app_version: str = "1.0.0",
        build_id: str | None = None,
        os_version: str | None = None,
        crash_time: str | None = None,
        use_header: bool = True,
        project_id: str | None = None,
    ) -> Any:
        data: dict[str, str] = {}
        if app_version is not None:
            data["app_version"] = app_version
        if project_id is not None:
            data["project_id"] = project_id
        elif project_key is not None:
            data["project_key"] = project_key
        if build_id is not None:
            data["build_id"] = build_id
        if os_version is not None:
            data["os_version"] = os_version
        if crash_time is not None:
            data["crash_time"] = crash_time

        headers: dict[str, str] = {}
        if api_key is not None:
            if use_header:
                headers["X-API-Key"] = api_key
            else:
                data["api_key"] = api_key

        files: list[tuple[str, tuple[str, bytes, str]]] = []
        if dump_bytes is not None:
            files.append(("dump_file", (dump_filename, dump_bytes, "application/octet-stream")))
        for fname, content in log_files or []:
            files.append(("log_files", (fname, content, "text/plain")))

        return await client.post("/api/v1/crashes/upload", data=data, files=files, headers=headers)

    async def wait_for_crash_status(
        self,
        client: Any,
        crash_id: str,
        target_statuses: tuple[str, ...] = ("ANALYZED", "FAILED"),
        timeout: float = 15.0,
        poll_interval: float = 0.05,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_body: Any = None
        while time.monotonic() < deadline:
            resp = await client.get(f"/api/v1/crashes/{crash_id}")
            if resp.status_code == 200:
                last_body = resp.json()
                if last_body.get("status") in target_statuses:
                    return last_body
            await asyncio.sleep(poll_interval)
        raise TimeoutError(
            f"Crash {crash_id} did not reach {target_statuses} within {timeout}s; last={last_body}"
        )

    async def stream_crash_sse(
        self,
        client: Any,
        crash_id: str,
        timeout: float = 15.0,
    ) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []

        async def _consume() -> None:
            async with client.stream(
                "GET",
                f"/api/v1/crashes/{crash_id}/events",
                headers={"Accept": "text/event-stream"},
            ) as resp:
                assert resp.status_code == 200, f"SSE status {resp.status_code}"
                ctype = resp.headers.get("content-type", "")
                assert "text/event-stream" in ctype, f"Unexpected SSE content-type: {ctype}"
                current_event = "message"
                data_lines: list[str] = []
                async for line in resp.aiter_lines():
                    if line == "":
                        if data_lines:
                            raw_data = "\n".join(data_lines)
                            parsed = json.loads(raw_data)
                            events.append((current_event, parsed))
                            if parsed.get("status") in ("ANALYZED", "FAILED"):
                                return
                        current_event = "message"
                        data_lines = []
                    elif line.startswith(":"):
                        continue
                    elif line.startswith("event:"):
                        current_event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line.split(":", 1)[1].strip())
                if data_lines:
                    raw_data = "\n".join(data_lines)
                    parsed = json.loads(raw_data)
                    events.append((current_event, parsed))

        await asyncio.wait_for(_consume(), timeout=timeout)
        return events

    def extract_log_id(self, tool_content: str, preferred_filename: str | None = None) -> str:
        try:
            payload = json.loads(tool_content)
        except json.JSONDecodeError:
            payload = None

        if isinstance(payload, dict):
            logs = payload.get("logs")
            if isinstance(logs, list):
                if not logs:
                    return ""
                if preferred_filename:
                    for entry in logs:
                        if isinstance(entry, dict) and entry.get("filename") == preferred_filename:
                            lid = entry.get("id") or entry.get("log_id")
                            if lid:
                                return str(lid)
                first = logs[0]
                if isinstance(first, dict):
                    lid = first.get("id") or first.get("log_id")
                    if lid:
                        return str(lid)
                return ""

        m = re.search(
            r'"logs"\s*:\s*\[[^\]]*?\"(?:id|log_id)\"\s*:\s*'
            r'"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"',
            tool_content,
            re.IGNORECASE | re.DOTALL,
        )
        return m.group(1) if m else ""

    def make_mixed_encoding_log(
        self,
        num_lines: int = 120,
        include_bom: bool = True,
        include_gbk: bool = True,
        include_invalid_bytes: bool = False,
        base_time: str = "2026-11-22T10:00:00",
        error_lines: dict[int, str] | None = None,
    ) -> bytes:
        base_dt = dt.datetime.fromisoformat(base_time)
        chunks: list[bytes] = []
        if include_bom:
            chunks.append(b"\xef\xbb\xbf")

        custom_errors = error_lines or {}
        for idx in range(1, num_lines + 1):
            ts = (base_dt + dt.timedelta(seconds=idx)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            if idx in custom_errors:
                line_bytes = f"{ts} {custom_errors[idx]}\n".encode()
            elif include_gbk and idx % 7 == 0:
                line_bytes = (
                    f"{ts} ERROR 纹理加载失败 TextureCache fallback null 𠮷 第{idx}行\n"
                ).encode("gb18030")
            elif idx % 15 == 0:
                line_bytes = (
                    f"{ts} FATAL Renderer::SubmitDrawCall null texture pointer at line {idx}\n"
                ).encode()
            elif idx % 5 == 0:
                line_bytes = f"{ts} WARN Frame budget exceeded line={idx}\n".encode()
            else:
                line_bytes = f"{ts} INFO Worker heartbeat tick={idx} ok\n".encode()

            if include_invalid_bytes and idx == num_lines // 2:
                line_bytes = (
                    f"{ts} WARN Corrupted bytes ".encode() + b"\xff\xfe\x80" + b" recovered\n"
                )
            chunks.append(line_bytes)

        return b"".join(chunks)

    def age_files(self, root_dir: Path, days_old: int = 40) -> int:
        if not root_dir.exists():
            return 0
        target_ts = time.time() - days_old * 86400
        count = 0
        for p in root_dir.rglob("*"):
            if p.is_file():
                os.utime(p, (target_ts, target_ts))
                count += 1
        return count

    def run_cli(
        self,
        args: list[str],
        env: dict[str, str],
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        if not CLI_SCRIPT.exists():
            msg = "cli/crashkiller_cli.py not yet implemented"
            if require_app_flag():
                pytest.fail(msg)
            pytest.skip(msg)
        return subprocess.run(
            [sys.executable, str(CLI_SCRIPT), *args],
            env=env,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
