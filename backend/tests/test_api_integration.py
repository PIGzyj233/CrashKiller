"""Full-stack FastAPI REST, SSE, SymSrv, KB verdict, and GC integration tests."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import event

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


@pytest.fixture
async def api_harness(tmp_path: Path):
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


async def test_over_quota_uploads_do_not_leave_artifacts(api_harness: Any) -> None:
    client = api_harness.client
    project = (await client.post("/api/v1/projects", json={"key": "quota", "name": "Quota"})).json()
    response = await client.put("/api/v1/settings", json={"total_disk_quota_gb": 16 / (1024**3)})
    assert response.status_code == 200

    response = await client.post(
        "/api/v1/crashes/upload",
        data={"project_key": "quota", "app_version": "1.0"},
        files={"dump_file": ("sample.dmp", bytes(range(256)))},
        headers={"X-API-Key": project["api_key"]},
    )

    assert response.status_code == 507
    assert not list((api_harness.data_dir / "dumps").rglob("*.zst"))
    assert (await client.get("/api/v1/crashes")).json()["total"] == 0
    response = await client.put(
        "/api/v1/settings",
        json={"total_disk_quota_gb": 150, "symbols_sym_quota_gb": 16 / (1024**3)},
    )
    assert response.status_code == 200
    response = await client.post(
        "/api/v1/symbols/upload",
        data={"project_key": "quota", "app_version": "1.0"},
        files={"pdb_file": ("sample.pdb", b"PDB fixture")},
        headers={"X-API-Key": project["api_key"]},
    )
    assert response.status_code == 507
    assert not list((api_harness.data_dir / "symbols" / "sym").rglob("*.sym"))
    assert (await client.get("/api/v1/symbols")).json()["total"] == 0


async def test_admin_auth_denies_anonymous_access_and_untrusted_origin(
    api_harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = api_harness.client
    assert (await client.get("/health", auth=None)).status_code == 200
    anonymous = await client.get("/api/v1/projects", auth=None)
    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"] == 'Basic realm="CrashKiller", charset="UTF-8"'
    assert (await client.get("/internal/admin-auth", auth=None)).status_code == 401
    assert (await client.get("/internal/admin-auth")).status_code == 204
    assert (await client.post("/api/v1/settings/gc", auth=None)).status_code == 401
    assert (await client.get("/api/v1/settings", auth=("wrong", "wrong"))).status_code == 401
    assert (await client.get("/symsrv/pingme.txt", auth=None)).status_code == 401

    project = {"key": "auth-origin", "name": "Auth Origin"}
    assert (
        await client.post(
            "/api/v1/projects", json=project, headers={"Origin": "https://other.example"}
        )
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/projects", json=project, headers={"Origin": "http://testserver"}
        )
    ).status_code == 201

    monkeypatch.setattr(api_harness.settings, "ALLOW_UNAUTHENTICATED_SYMSRV", True)
    assert (await client.get("/symsrv/pingme.txt", auth=None)).status_code == 200
    monkeypatch.setattr(api_harness.settings, "ADMIN_USERNAME", "")
    assert (await client.get("/api/v1/projects")).status_code == 403
    assert (await client.get("/internal/admin-auth")).status_code == 403


async def test_crash_status_scopes_project_keys_and_closes_db_before_sse_send(
    api_harness: Any,
) -> None:
    client = api_harness.client
    owner = (
        await client.post(
            "/api/v1/projects",
            json={"key": "owner", "name": "Owner", "settings_json": {"ai_triage_mode": "MANUAL"}},
        )
    ).json()
    other = (await client.post("/api/v1/projects", json={"key": "other", "name": "Other"})).json()
    own_key = {"X-API-Key": owner["api_key"]}
    uploaded = await client.post(
        "/api/v1/crashes/upload",
        auth=None,
        headers=own_key,
        data={"project_key": owner["key"], "app_version": "1.0.0"},
        files={"dump_file": ("sample.dmp", b"MDMP_TEST_FIXTURE")},
    )
    assert uploaded.status_code == 202
    crash_id = uploaded.json()["crash_id"]
    await _drain(api_harness)

    detail_url = f"/api/v1/crashes/{crash_id}"
    status_url = f"{detail_url}/status"
    event_url = f"{detail_url}/events"
    assert (await client.get(detail_url, auth=None, headers=own_key)).status_code == 401
    for path in (status_url, event_url):
        assert (await client.get(path, auth=None)).status_code == 401
        wrong_key = {"X-API-Key": other["api_key"]}
        assert (await client.get(path, auth=None, headers=wrong_key)).status_code == 403

    snapshot = await client.get(status_url, auth=None, headers=own_key)
    assert snapshot.status_code == 200
    assert snapshot.headers["cache-control"] == "no-store"
    assert snapshot.json() == {
        "crash_id": crash_id,
        "status": "ANALYZED",
        "ai_verdict": None,
        "error": None,
    }

    pool = api_harness.app.state.engine.sync_engine.pool
    checked_out_count = 0
    counts_during_send: list[int] = []

    @event.listens_for(pool, "checkout")
    def count_checkout(*_: Any) -> None:
        nonlocal checked_out_count
        checked_out_count += 1

    @event.listens_for(pool, "checkin")
    def count_checkin(*_: Any) -> None:
        nonlocal checked_out_count
        checked_out_count -= 1

    async def observe_send(scope: Any, receive: Any, send: Any) -> None:
        async def record(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                counts_during_send.append(checked_out_count)
            await send(message)

        await api_harness.app(scope, receive, record)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=observe_send), base_url="http://testserver"
    ) as machine_client:
        response = await machine_client.get(event_url, headers=own_key)
    assert response.status_code == 200
    assert '"status": "ANALYZED"' in response.text
    assert counts_during_send == [0]


async def test_health_projects_auth_and_key_rotation(api_harness: Any) -> None:
    client: httpx.AsyncClient = api_harness.client

    assert (await client.get("/health")).json() == {"status": "ok"}

    p_resp = await client.post(
        "/api/v1/projects",
        json={"key": "game-alpha", "name": "Game Alpha"},
    )
    assert p_resp.status_code == 201
    proj = p_resp.json()
    assert len(proj["api_key"]) >= 16
    assert "api_key_hash" not in proj

    dup = await client.post(
        "/api/v1/projects",
        json={"key": "game-alpha", "name": "Dup"},
    )
    assert dup.status_code == 409

    listed = await client.get("/api/v1/projects")
    assert listed.status_code == 200
    assert isinstance(listed.json(), list)
    assert all("api_key" not in item and "api_key_hash" not in item for item in listed.json())

    # Upload auth error cases: 404 unknown project, 401 missing key, 403 bad key
    assert (
        await client.post(
            "/api/v1/symbols/upload",
            data={"project_key": "missing-proj", "app_version": "1.0.0"},
            files={"pdb_file": ("a.pdb", b"PDBDATA")},
        )
    ).status_code == 404
    assert (
        await client.post(
            "/api/v1/symbols/upload",
            data={"project_key": "game-alpha", "app_version": "1.0.0"},
            files={"pdb_file": ("a.pdb", b"PDBDATA")},
        )
    ).status_code == 401
    assert (
        await client.post(
            "/api/v1/symbols/upload",
            headers={"X-API-Key": "wrong-key"},
            data={"project_key": "game-alpha", "app_version": "1.0.0"},
            files={"pdb_file": ("a.pdb", b"PDBDATA")},
        )
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/symbols/upload",
            headers={"X-API-Key": proj["api_key"]},
            data={
                "project_key": "game-alpha",
                "app_version": "1.0.0",
                "api_key": "wrong-key",
            },
            files={"pdb_file": ("a.pdb", b"PDBDATA")},
        )
    ).status_code == 403

    rot = await client.post(f"/api/v1/projects/{proj['id']}/rotate-key")
    assert rot.status_code == 200
    new_key = rot.json()["api_key"]
    assert new_key != proj["api_key"]

    old_try = await client.post(
        "/api/v1/symbols/upload",
        headers={"X-API-Key": proj["api_key"]},
        data={"project_key": "game-alpha", "app_version": "1.0.0"},
        files={"pdb_file": ("Core.pdb", b"VALID_PDB")},
    )
    assert old_try.status_code == 403

    new_try = await client.post(
        "/api/v1/symbols/upload",
        data={"project_key": "game-alpha", "app_version": "1.0.0", "api_key": new_key},
        files={"pdb_file": ("Core.pdb", b"VALID_PDB")},
    )
    assert new_try.status_code == 201


async def test_project_api_key_retrieval_and_legacy_sync(api_harness: Any) -> None:
    from app.db.models import Project

    client: httpx.AsyncClient = api_harness.client

    created = (
        await client.post(
            "/api/v1/projects",
            json={"key": "key-inspect", "name": "Key Inspect"},
        )
    ).json()
    project_id = created["id"]
    initial_key = created["api_key"]

    # List endpoint continues to omit secrets
    listed = (await client.get("/api/v1/projects")).json()
    assert all("api_key" not in item and "api_key_hash" not in item for item in listed)

    # Admin can fetch the stored key directly with no-store cache headers
    key_resp = await client.get(f"/api/v1/projects/{project_id}/api-key")
    assert key_resp.status_code == 200
    assert key_resp.headers.get("cache-control") == "no-store"
    assert key_resp.json() == {
        "id": project_id,
        "key": "key-inspect",
        "api_key": initial_key,
    }

    # Anonymous access is rejected
    anon_resp = await client.get(f"/api/v1/projects/{project_id}/api-key", auth=None)
    assert anon_resp.status_code == 401

    # Unknown project returns 404
    missing_resp = await client.get(f"/api/v1/projects/{uuid.uuid4()}/api-key")
    assert missing_resp.status_code == 404

    # Rotating updates the retrievable key
    rot_resp = await client.post(f"/api/v1/projects/{project_id}/rotate-key")
    assert rot_resp.status_code == 200
    rotated_key = rot_resp.json()["api_key"]
    assert rotated_key != initial_key

    after_rot = await client.get(f"/api/v1/projects/{project_id}/api-key")
    assert after_rot.status_code == 200
    assert after_rot.json()["api_key"] == rotated_key

    # Simulate a legacy pre-migration project with api_key=None in DB
    async with api_harness.app.state.session_factory() as session:
        row = await session.get(Project, uuid.UUID(project_id))
        assert row is not None
        row.api_key = None
        await session.commit()

    legacy_resp = await client.get(f"/api/v1/projects/{project_id}/api-key")
    assert legacy_resp.status_code == 200
    assert legacy_resp.json()["api_key"] is None

    # Sync rejects a mismatched key
    bad_sync = await client.post(
        f"/api/v1/projects/{project_id}/api-key/sync",
        json={"api_key": initial_key},
    )
    assert bad_sync.status_code == 400

    # Sync accepts the matching key and persists it for future lookups
    good_sync = await client.post(
        f"/api/v1/projects/{project_id}/api-key/sync",
        json={"api_key": rotated_key},
    )
    assert good_sync.status_code == 200
    assert good_sync.headers.get("cache-control") == "no-store"
    assert good_sync.json()["api_key"] == rotated_key

    after_sync = await client.get(f"/api/v1/projects/{project_id}/api-key")
    assert after_sync.status_code == 200
    assert after_sync.json()["api_key"] == rotated_key


async def test_symbol_upload_dedup_symsrv_and_refcounted_delete(api_harness: Any) -> None:
    client: httpx.AsyncClient = api_harness.client

    assert (await client.get("/symsrv/pingme.txt")).text == ""
    assert (await client.get("/symsrv/index2.txt")).status_code == 404

    p1 = (await client.post("/api/v1/projects", json={"key": "p1", "name": "P1"})).json()
    p2 = (await client.post("/api/v1/projects", json={"key": "p2", "name": "P2"})).json()

    pdb_bytes = b"RAW_CORE_PDB_BYTES_001"
    pe_bytes = b"RAW_CORE_PE_BYTES_001"

    # First upload PDB-only (has_pe_unwind=False)
    r1 = await client.post(
        "/api/v1/symbols/upload",
        headers={"X-API-Key": p1["api_key"]},
        data={"project_key": p1["key"], "app_version": "1.0.0"},
        files={"pdb_file": ("Core.pdb", pdb_bytes)},
    )
    assert r1.status_code == 201
    s1 = r1.json()
    assert s1["has_pe_unwind"] is False
    assert s1["deduplicated"] is False

    # Re-upload same PDB with PE -> upgrades has_pe_unwind=True and deduplicated=True
    r2 = await client.post(
        "/api/v1/symbols/upload",
        headers={"X-API-Key": p1["api_key"]},
        data={"project_key": p1["key"], "app_version": "1.0.0"},
        files={"pdb_file": ("Core.pdb", pdb_bytes), "pe_file": ("Core.dll", pe_bytes)},
    )
    assert r2.status_code == 201
    s2 = r2.json()
    assert s2["id"] == s1["id"]
    assert s2["has_pe_unwind"] is True
    assert s2["deduplicated"] is True

    did = s2["debug_id"]
    head_resp = await client.head(f"/symsrv/Core.pdb/{did}/Core.pdb")
    assert head_resp.status_code == 200
    assert head_resp.content == b""

    get_resp = await client.get(f"/symsrv/Core.pdb/{did}/Core.pdb")
    assert get_resp.status_code == 200
    assert get_resp.content == pdb_bytes

    # Upload identical symbol to project 2 (isolated per-project .sym path, shared raw archive)
    r_p2 = await client.post(
        "/api/v1/symbols/upload",
        headers={"X-API-Key": p2["api_key"]},
        data={"project_key": p2["key"], "app_version": "1.0.0"},
        files={"pdb_file": ("Core.pdb", pdb_bytes), "pe_file": ("Core.dll", pe_bytes)},
    )
    assert r_p2.status_code == 201
    sym_file_p1 = api_harness.data_dir / s2["sym_path"]
    sym_file_p2 = api_harness.data_dir / r_p2.json()["sym_path"]
    assert s2["sym_path"] != r_p2.json()["sym_path"]
    assert sym_file_p1.is_file()
    assert sym_file_p2.is_file()

    # Deleting p1's symbol row removes p1's .sym while preserving p2's .sym & shared raw archive
    assert (await client.delete(f"/api/v1/symbols/{s2['id']}")).status_code == 204
    assert not sym_file_p1.exists()
    assert sym_file_p2.is_file()
    assert (await client.get(f"/symsrv/Core.pdb/{did}/Core.pdb")).status_code == 200

    # Deleting p2's symbol row removes the last reference -> .sym & raw archive unlinked
    assert (await client.delete(f"/api/v1/symbols/{r_p2.json()['id']}")).status_code == 204
    assert not sym_file_p2.exists()
    assert (await client.get(f"/symsrv/Core.pdb/{did}/Core.pdb")).status_code == 404


async def test_crash_pipeline_sse_logs_verdict_and_issues_merge_split(api_harness: Any) -> None:
    client: httpx.AsyncClient = api_harness.client
    proj = (
        await client.post(
            "/api/v1/projects",
            json={"key": "crash-flow", "name": "Crash Flow"},
        )
    ).json()

    log_payload = (
        b"\xef\xbb\xbf2026-11-22 10:00:00.000 INFO startup ok\n"
        + "2026-11-22 10:00:05.000 ERROR ERR_RENDER_NULL 纹理为空\n".encode("gbk")
        + b"2026-11-22 10:00:08.000 FATAL aborting process\n"
    )
    api_harness.openai_client.queue_verdict(verdict="NEW_ISSUE", suggested_title="Core Render Null")

    up = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj["api_key"]},
        data={
            "project_key": proj["key"],
            "app_version": "2.0.0",
            "build_id": "b100",
            "os_version": "Windows 11",
            "crash_time": "2026-11-22T10:00:09Z",
        },
        files=[
            ("dump_file", ("crash.dmp", b"MDMP_VALID_HEADER_DATA", "application/octet-stream")),
            ("log_files", ("game.log", log_payload, "text/plain")),
        ],
    )
    assert up.status_code == 202
    crash_id = up.json()["crash_id"]
    log_id = up.json()["log_ids"][0]

    await _drain(api_harness)

    # Late-connect SSE on ANALYZED crash emits terminal snapshot
    async with client.stream("GET", f"/api/v1/crashes/{crash_id}/events") as sse_resp:
        assert sse_resp.status_code == 200
        body = (await sse_resp.aread()).decode("utf-8")
        assert "event: status" in body
        assert '"status": "ANALYZED"' in body

    detail = (await client.get(f"/api/v1/crashes/{crash_id}")).json()
    assert detail["status"] == "ANALYZED"
    assert detail["dump_path"] is not None
    assert detail["ai_verdict"] == "NEW_ISSUE"
    assert len(detail["ai_runs"]) == 1
    issue_id = detail["issue"]["id"]

    # Missing symbols queue handles debug_id=None safely without 500
    missing = await client.get("/api/v1/symbols/missing", params={"project_id": proj["id"]})
    assert missing.status_code == 200
    assert isinstance(missing.json(), list)
    assert any(m["debug_file"] == "NoDebugId.pdb" and m["debug_id"] == "" for m in missing.json())

    # Logs endpoints
    summary = (await client.get(f"/api/v1/logs/{log_id}/summary")).json()
    assert summary["total_lines"] == 3
    assert summary["digest"]["error_count"] >= 2

    lines = (
        await client.get(f"/api/v1/logs/{log_id}/lines", params={"start": 1, "end": 10})
    ).json()
    assert len(lines["lines"]) == 3
    assert "纹理为空" in lines["lines"][1]["text"]

    around = (
        await client.get(
            f"/api/v1/logs/{log_id}/around-time",
            params={"timestamp": "2026-11-22T10:00:05Z", "window_sec": 2},
        )
    ).json()
    assert around["window_sec"] == 2
    assert any("ERR_RENDER_NULL" in item["text"] for item in around["lines"])

    search = (
        await client.get(
            f"/api/v1/logs/{log_id}/search",
            params={"pattern": "ERR_.*NULL", "is_regex": "true", "max_matches": 200},
        )
    ).json()
    assert search["total_matches"] == 1
    assert (
        await client.get(
            f"/api/v1/logs/{log_id}/search",
            params={"pattern": "[bad", "is_regex": "true"},
        )
    ).status_code == 422

    # Artifacts
    dmp_dl = await client.get(f"/api/v1/crashes/{crash_id}/artifacts/dump")
    assert dmp_dl.status_code == 200
    assert dmp_dl.content == b"MDMP_VALID_HEADER_DATA"
    stacks_resp = await client.get(f"/api/v1/crashes/{crash_id}/artifacts/stacks_json")
    assert stacks_resp.status_code == 200

    # Human verdict KB feedback (confirm + create_issue)
    verdict_resp = await client.post(
        f"/api/v1/crashes/{crash_id}/verdict",
        json={
            "action": "confirm",
            "root_cause": "Verified GPU resource race.",
            "add_log_patterns": ["ERR_RENDER_NULL"],
        },
    )
    assert verdict_resp.status_code == 200
    assert verdict_resp.json()["is_human_verified"] is True
    assert verdict_resp.json()["issue"]["status"] == "KNOWN"

    # Manual issue + merge + invalid self-merge check
    manual_issue = (
        await client.post(
            "/api/v1/issues",
            json={"project_id": proj["id"], "title": "Merge Target", "status": "KNOWN"},
        )
    ).json()
    assert (
        await client.post(
            "/api/v1/issues/merge",
            json={"target_issue_id": manual_issue["id"], "source_issue_ids": [manual_issue["id"]]},
        )
    ).status_code == 400
    merged = (
        await client.post(
            "/api/v1/issues/merge",
            json={"target_issue_id": manual_issue["id"], "source_issue_ids": [issue_id]},
        )
    ).json()
    assert merged["crash_count"] == 1
    assert len(merged["clusters"]) == 1

    split_resp = await client.post(
        f"/api/v1/issues/{manual_issue['id']}/split",
        json={
            "cluster_id": merged["clusters"][0]["id"],
            "new_issue_title": "Split Out Cluster",
        },
    )
    assert split_resp.status_code == 200
    split_data = split_resp.json()
    assert split_data["source_issue"]["crash_count"] == 0
    assert split_data["target_issue"]["crash_count"] == 1
    assert split_data["target_issue"]["title"] == "Split Out Cluster"
    assert split_data["cluster"]["issue_id"] == split_data["target_issue"]["id"]

    # Corrupt dump -> FAILED + late-connect SSE includes non-empty error
    bad_up = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj["api_key"]},
        data={"project_key": proj["key"], "app_version": "2.0.0"},
        files={"dump_file": ("bad.dmp", b"CORRUPT_DUMP", "application/octet-stream")},
    )
    assert bad_up.status_code == 202
    bad_id = bad_up.json()["crash_id"]
    await _drain(api_harness)

    async with client.stream("GET", f"/api/v1/crashes/{bad_id}/events") as bad_sse:
        bad_body = (await bad_sse.aread()).decode("utf-8")
        assert '"status": "FAILED"' in bad_body
        data_line = [ln for ln in bad_body.splitlines() if ln.startswith("data:")][0]
        evt = json.loads(data_line[5:].strip())
        assert evt["error"]


async def test_gc_protects_pinned_and_enforces_50gb_safety_floor(api_harness: Any) -> None:
    client: httpx.AsyncClient = api_harness.client
    proj = (
        await client.post("/api/v1/projects", json={"key": "gc-proj", "name": "GC Proj"})
    ).json()

    # Upload two crashes with logs
    api_harness.openai_client.queue_verdict(verdict="NEW_ISSUE", suggested_title="Pinned Issue")
    up_pin = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj["api_key"]},
        data={"project_key": proj["key"], "app_version": "1.0.0"},
        files=[
            ("dump_file", ("pin.dmp", b"MDMP_PINNED", "application/octet-stream")),
            ("log_files", ("pin.log", b"2026-11-22 10:00:00.000 INFO pinned\n", "text/plain")),
        ],
    )
    pin_cid = up_pin.json()["crash_id"]
    pin_lid = up_pin.json()["log_ids"][0]

    api_harness.openai_client.queue_verdict(verdict="NEW_ISSUE", suggested_title="Evictable Issue")
    up_free = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj["api_key"]},
        data={"project_key": proj["key"], "app_version": "1.0.0"},
        files=[
            ("dump_file", ("free.dmp", b"MDMP_FREE", "application/octet-stream")),
            ("log_files", ("free.log", b"2026-11-22 10:00:00.000 INFO free\n", "text/plain")),
        ],
    )
    free_cid = up_free.json()["crash_id"]
    free_lid = up_free.json()["log_ids"][0]
    await _drain(api_harness)

    pin_resp = await client.patch(f"/api/v1/crashes/{pin_cid}/pin", json={"is_pinned": True})
    assert pin_resp.status_code == 200

    # Simulate host free space falling below 50GB safety floor
    api_harness.disk_state["free_gb"] = 40.0
    await client.put(
        "/api/v1/settings",
        json={"archive_raw_symbols": True, "host_safety_floor_gb": 50.0},
    )

    # Raw symbol archiving must pause when host floor is breached
    sym_resp = await client.post(
        "/api/v1/symbols/upload",
        headers={"X-API-Key": proj["api_key"]},
        data={"project_key": proj["key"], "app_version": "1.0.0"},
        files={"pdb_file": ("Floor.pdb", b"VALID_PDB")},
    )
    assert sym_resp.status_code == 201
    assert sym_resp.json()["raw_size"] == 0

    dash = (await client.get("/api/v1/dashboard", params={"project_id": proj["id"]})).json()
    assert dash["storage"]["safety_floor_breached"] is True
    assert dash["kpis"]["total_crashes"] == 2

    # GC triggers emergency cleanup, protects pinned crash, evicts unpinned crash files
    gc_resp = await client.post("/api/v1/settings/gc", json={"force_emergency": False})
    assert gc_resp.status_code == 200
    gc = gc_resp.json()
    assert gc["emergency_triggered"] is True
    assert gc["skipped_pinned_count"] >= 1
    assert gc["deleted_files_count"] > 0

    assert (await client.get(f"/api/v1/crashes/{pin_cid}/artifacts/dump")).status_code == 200
    pin_lines = await client.get(f"/api/v1/logs/{pin_lid}/lines", params={"start": 1, "end": 5})
    assert pin_lines.status_code == 200

    assert (await client.get(f"/api/v1/crashes/{free_cid}/artifacts/dump")).status_code == 404
    free_lines = await client.get(f"/api/v1/logs/{free_lid}/lines", params={"start": 1, "end": 5})
    assert free_lines.status_code == 410
    assert (await client.get(f"/api/v1/logs/{free_lid}/summary")).status_code == 200
    reproc = await client.post(f"/api/v1/crashes/{free_cid}/reprocess", json={"mode": "full"})
    assert reproc.status_code == 409
    assert uuid.UUID(free_cid)


async def test_crash_upload_sanitizes_log_filenames_and_cleans_partial_artifacts(
    api_harness: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.log_engine import LogEngine

    client: httpx.AsyncClient = api_harness.client
    proj = (
        await client.post(
            "/api/v1/projects",
            json={"key": "sanitize-proj", "name": "Sanitize Proj"},
        )
    ).json()

    # Conflicting X-API-Key header vs api_key form field -> 403
    conflict_resp = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj["api_key"]},
        data={
            "project_key": proj["key"],
            "app_version": "1.0.0",
            "api_key": "mismatched-form-key",
        },
        files={"dump_file": ("crash.dmp", b"MDMP_VALID", "application/octet-stream")},
    )
    assert conflict_resp.status_code == 403

    # Raw multipart payload with NUL byte and dot-dot traversal in log_files filenames
    boundary = "----CrashKillerRawBoundary98765"
    raw_multipart = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="project_key"\r\n\r\n{proj["key"]}\r\n'
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="app_version"\r\n\r\n1.0.0\r\n'
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="dump_file"; filename="crash.dmp"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
        "MDMP_VALID_HEADER_DATA\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="log_files"; filename="evil\x00.log"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "2026-11-22 10:00:00.000 INFO evil\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="log_files"; filename="../../.."\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "2026-11-22 10:00:01.000 INFO dotdot\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    up_resp = await client.post(
        "/api/v1/crashes/upload",
        headers={
            "X-API-Key": proj["api_key"],
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        content=raw_multipart,
    )
    assert up_resp.status_code == 202
    crash_id = up_resp.json()["crash_id"]
    await _drain(api_harness)

    detail = (await client.get(f"/api/v1/crashes/{crash_id}")).json()
    stored_names = [lg["filename"] for lg in detail["logs"]]
    assert len(stored_names) == 2
    for fname in stored_names:
        assert "\x00" not in fname
        assert fname not in {"", ".", ".."}
    assert "evil.log" in stored_names
    assert any(fname.endswith(".log") and fname != "evil.log" for fname in stored_names)

    # Partial failure during log ingestion cleans up dumps/<crash_id> and logs/<crash_id>
    dumps_dir = api_harness.data_dir / "dumps"
    logs_dir = api_harness.data_dir / "logs"
    dumps_before = sorted(p.relative_to(api_harness.data_dir) for p in dumps_dir.rglob("*"))
    logs_before = sorted(p.relative_to(api_harness.data_dir) for p in logs_dir.rglob("*"))

    orig_ingest = LogEngine.ingest_log
    call_count = 0

    def _fail_second_log(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise OSError("Simulated disk write failure")
        return orig_ingest(self, *args, **kwargs)

    monkeypatch.setattr(LogEngine, "ingest_log", _fail_second_log)
    fail_resp = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj["api_key"]},
        data={"project_key": proj["key"], "app_version": "1.0.0"},
        files=[
            ("dump_file", ("partial.dmp", b"MDMP_PARTIAL", "application/octet-stream")),
            ("log_files", ("first.log", b"2026-11-22 10:00:00.000 INFO ok\n", "text/plain")),
            ("log_files", ("second.log", b"2026-11-22 10:00:01.000 INFO fail\n", "text/plain")),
        ],
    )
    assert fail_resp.status_code == 507

    dumps_after = sorted(p.relative_to(api_harness.data_dir) for p in dumps_dir.rglob("*"))
    logs_after = sorted(p.relative_to(api_harness.data_dir) for p in logs_dir.rglob("*"))
    assert dumps_after == dumps_before
    assert logs_after == logs_before


async def test_settings_llm_providers_catalog_and_thinking_level_api(
    api_harness: Any,
) -> None:
    client: httpx.AsyncClient = api_harness.client

    providers_resp = await client.get("/api/v1/settings/llm-providers")
    assert providers_resp.status_code == 200
    providers = providers_resp.json()
    provider_ids = {p["id"] for p in providers}
    assert {"openai", "anthropic", "gemini", "deepseek", "ollama", "custom"}.issubset(
        provider_ids
    )
    anthropic_preset = next(p for p in providers if p["id"] == "anthropic")
    assert anthropic_preset["protocol"] == "anthropic_messages"
    assert anthropic_preset["supports_thinking"] is True
    assert len(anthropic_preset["models"]) >= 3

    put_resp = await client.put(
        "/api/v1/settings",
        json={
            "llm_provider": "anthropic",
            "llm_base_url": "https://api.anthropic.com",
            "llm_model": "claude-sonnet-4-5",
            "llm_thinking_level": "medium",
        },
    )
    assert put_resp.status_code == 200
    body = put_resp.json()
    assert body["llm_provider"] == "anthropic"
    assert body["llm_model"] == "claude-sonnet-4-5"
    assert body["llm_thinking_level"] == "medium"

    get_body = (await client.get("/api/v1/settings")).json()
    assert get_body["llm_provider"] == "anthropic"
    assert get_body["llm_thinking_level"] == "medium"

    bad_resp = await client.put(
        "/api/v1/settings",
        json={"llm_thinking_level": "ultra"},
    )
    assert bad_resp.status_code == 422


async def test_crash_session_id_upload_filter_related_logs_and_project_label(
    api_harness: Any,
) -> None:
    import sys
    from pathlib import Path

    client: httpx.AsyncClient = api_harness.client

    # 1. Project with custom session_id_label + validation
    proj_a_resp = await client.post(
        "/api/v1/projects",
        json={
            "key": "sess-proj-a",
            "name": "Session Project A",
            "settings_json": {"session_id_label": "  FlowID  "},
        },
    )
    assert proj_a_resp.status_code == 201
    proj_a = proj_a_resp.json()
    assert proj_a["settings_json"]["session_id_label"] == "FlowID"

    bad_label_resp = await client.patch(
        f"/api/v1/projects/{proj_a['id']}",
        json={"settings_json": {"session_id_label": "x" * 65}},
    )
    assert bad_label_resp.status_code == 422

    proj_b_resp = await client.post(
        "/api/v1/projects",
        json={"key": "sess-proj-b", "name": "Session Project B"},
    )
    assert proj_b_resp.status_code == 201
    proj_b = proj_b_resp.json()

    # 2. Invalid session_id rejected on upload
    bad_upload = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj_a["api_key"]},
        data={
            "project_key": proj_a["key"],
            "app_version": "2.0.0",
            "session_id": "bad\nline",
        },
        files={"dump_file": ("crash.dmp", b"MDMP_TEST", "application/octet-stream")},
    )
    assert bad_upload.status_code == 422

    # 3. Upload crash #1 with mid-file restart log + session_id
    log_text = "\n".join(
        [
            "2026-11-24 09:50:00.000 INFO [other-flow-1] pre-crash noise",
            "2026-11-24 10:00:01.000 INFO [flow-shared-99] session start",
            "2026-11-24 10:00:05.000 ERROR [flow-shared-99] fatal GPU hang",
            "2026-11-24 10:00:05.200 FATAL Unhandled exception dump start",
            "2026-11-24 10:00:09.000 INFO === Process restarted ===",
            "2026-11-24 10:05:00.000 ERROR [after-restart-2] unrelated error after restart",
        ]
    ).encode("utf-8")

    up1 = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj_a["api_key"]},
        data={
            "project_key": proj_a["key"],
            "app_version": "2.0.0",
            "session_id": "  flow-shared-99  ",
        },
        files=[
            ("dump_file", ("c1.dmp", b"MDMP_ONE", "application/octet-stream")),
            ("log_files", ("runtime.log", log_text, "text/plain")),
        ],
    )
    assert up1.status_code == 202
    c1_id = up1.json()["crash_id"]
    log1_id = up1.json()["log_ids"][0]

    # Upload crash #2 in SAME project with SAME session_id
    up2 = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj_a["api_key"]},
        data={
            "project_key": proj_a["key"],
            "app_version": "2.0.0",
            "session_id": "flow-shared-99",
        },
        files={"dump_file": ("c2.dmp", b"MDMP_TWO", "application/octet-stream")},
    )
    assert up2.status_code == 202
    c2_id = up2.json()["crash_id"]

    # Upload crash #3 in OTHER project with SAME session_id (must NOT leak into related crashes)
    up3 = await client.post(
        "/api/v1/crashes/upload",
        headers={"X-API-Key": proj_b["api_key"]},
        data={
            "project_key": proj_b["key"],
            "app_version": "2.0.0",
            "session_id": "flow-shared-99",
        },
        files={"dump_file": ("c3.dmp", b"MDMP_THREE", "application/octet-stream")},
    )
    assert up3.status_code == 202
    c3_id = up3.json()["crash_id"]

    await _drain(api_harness)

    # 4. Exact filter & fuzzy search by session_id
    exact_list = (
        await client.get(
            "/api/v1/crashes",
            params={"project_id": proj_a["id"], "session_id": "flow-shared-99"},
        )
    ).json()
    assert exact_list["total"] == 2
    assert {item["id"] for item in exact_list["items"]} == {c1_id, c2_id}
    assert all(item["session_id"] == "flow-shared-99" for item in exact_list["items"])

    fuzzy_list = (
        await client.get(
            "/api/v1/crashes",
            params={"project_id": proj_a["id"], "q": "shared-99"},
        )
    ).json()
    assert fuzzy_list["total"] == 2

    # 5. Crash detail includes session_id, mid-file session digest, and isolated related crashes
    detail = (await client.get(f"/api/v1/crashes/{c1_id}")).json()
    assert detail["session_id"] == "flow-shared-99"
    assert detail["related_session_crash_count"] == 1
    assert [r["id"] for r in detail["related_session_crashes"]] == [c2_id]
    assert c3_id not in {r["id"] for r in detail["related_session_crashes"]}
    sess_digest = detail["logs"][0]["digest_json"]["session"]
    assert sess_digest["match_count"] == 2
    assert sess_digest["first_line"] == 2
    assert sess_digest["last_line"] == 3
    assert sess_digest["trailing_lines_after_session"] == 3
    assert len(sess_digest["post_session_lines"]) == 3
    assert "Unhandled exception" in sess_digest["post_session_lines"][0]["text"]

    # 6. Log session-lines, session-scoped search, and session-scoped around-time
    sess_lines_resp = await client.get(
        f"/api/v1/logs/{log1_id}/session-lines",
        params={"start": 1, "end": 10},
    )
    assert sess_lines_resp.status_code == 200
    sl_body = sess_lines_resp.json()
    assert sl_body["session_id"] == "flow-shared-99"
    assert sl_body["total_matches"] == 2
    assert [ln["line_number"] for ln in sl_body["lines"]] == [2, 3]

    sess_search = (
        await client.get(
            f"/api/v1/logs/{log1_id}/search",
            params={"pattern": "ERROR", "only_session": "true"},
        )
    ).json()
    assert sess_search["session_id"] == "flow-shared-99"
    assert [m["line_number"] for m in sess_search["matches"]] == [3]

    sess_around = (
        await client.get(
            f"/api/v1/logs/{log1_id}/around-time",
            params={
                "timestamp": "2026-11-24T10:00:05Z",
                "window_sec": 10,
                "only_session": "true",
            },
        )
    ).json()
    assert sess_around["session_id"] == "flow-shared-99"
    assert [ln["line_number"] for ln in sess_around["lines"]] == [2, 3]

    # 7. CLI upload-crash parser accepts --session-id
    cli_dir = str(Path(__file__).resolve().parents[2] / "cli")
    if cli_dir not in sys.path:
        sys.path.insert(0, cli_dir)
    import crashkiller_cli

    parsed_cli = crashkiller_cli.build_parser().parse_args(
        [
            "upload-crash",
            "--server",
            "http://localhost:8080",
            "--project",
            "demo-game",
            "--api-key",
            "ck_key",
            "--app-version",
            "2.0.0",
            "--session-id",
            "flow-cli-01",
            "--dump",
            "c.dmp",
        ]
    )
    assert parsed_cli.session_id == "flow-cli-01"


async def test_public_symbol_store_upload_sync_missing_badges_and_delete(
    api_harness: Any,
) -> None:
    client: httpx.AsyncClient = api_harness.client

    proj = (
        await client.post(
            "/api/v1/projects",
            json={"key": "pub-demo", "name": "Public Demo"},
        )
    ).json()

    # 1. Manual public upload WITHOUT project_id, app_version, or project API key
    r_pub = await client.post(
        "/api/v1/symbols/public/upload",
        data={"auto_resymbolicate": "false"},
        files={"pdb_file": ("GraphicsCapture.pdb", b"RAW_GC_PDB_DATA")},
    )
    assert r_pub.status_code == 201
    pub_sym = r_pub.json()
    assert pub_sym["project_id"] is None
    assert pub_sym["app_version"] is None
    assert pub_sym["scope"] == "public"
    assert pub_sym["source"] == "upload"
    assert pub_sym["has_pe_unwind"] is False

    # Upgrade the public symbol with PE companion
    r_pub_up = await client.post(
        "/api/v1/symbols/public/upload",
        data={"auto_resymbolicate": "false"},
        files={
            "pdb_file": ("GraphicsCapture.pdb", b"RAW_GC_PDB_DATA"),
            "pe_file": ("GraphicsCapture.dll", b"RAW_GC_PE_DATA"),
        },
    )
    assert r_pub_up.status_code == 201
    up_sym = r_pub_up.json()
    assert up_sym["id"] == pub_sym["id"]
    assert up_sym["deduplicated"] is True
    assert up_sym["upgraded"] is True
    assert up_sym["has_pe_unwind"] is True

    # Direct .sym upload to public store
    direct_sym_did = "ABCDEF1234567890ABCDEF12345678901"
    direct_sym_bytes = (
        f"MODULE windows x86_64 {direct_sym_did} DirectHelper.pdb\n"
        "STACK CFI INIT 1000 20 .cfa: $rsp 8 +\n"
        "PUBLIC 1000 0 DirectFunc\n"
    ).encode()
    r_sym = await client.post(
        "/api/v1/symbols/public/upload",
        files={"pdb_file": ("DirectHelper.sym", direct_sym_bytes)},
    )
    assert r_sym.status_code == 201
    assert r_sym.json()["debug_id"] == direct_sym_did
    assert r_sym.json()["scope"] == "public"

    # Project inventory remains isolated from public symbol store
    proj_list = (
        await client.get("/api/v1/symbols", params={"scope": "project"})
    ).json()
    assert proj_list["total"] == 0

    pub_list = (
        await client.get("/api/v1/symbols", params={"scope": "public"})
    ).json()
    assert pub_list["total"] == 2
    assert all(item["project_id"] is None for item in pub_list["items"])

    # WinDbg /symsrv serves raw archived public symbol
    gc_did = up_sym["debug_id"]
    assert (
        await client.get(f"/symsrv/GraphicsCapture.pdb/{gc_did}/GraphicsCapture.pdb")
    ).status_code == 200

    # 2. Seed a crash with missing GraphicsCapture.pdb (Windows system module) & test MSDL sync
    sync_did = "90D28435EAAB109449C47402C53C42D31"
    from app.db.models import CrashReport

    async with api_harness.app.state.session_factory() as session:
        crash_id = uuid.uuid4()
        dmp_src = api_harness.data_dir / "tmp_sync.dmp"
        dmp_src.write_bytes(b"MDMP_TEST")
        dmp_rel, _, _ = api_harness.app.state.storage.compress_dump(dmp_src, crash_id)
        dmp_src.unlink(missing_ok=True)
        session.add(
            CrashReport(
                id=crash_id,
                project_id=uuid.UUID(proj["id"]),
                status="ANALYZED",
                app_version="1.0.0",
                dump_path=dmp_rel,
                missing_symbols_json=[
                    {
                        "module_name": "GraphicsCapture.dll",
                        "debug_file": "GraphicsCapture.pdb",
                        "debug_id": sync_did,
                        "code_file": r"C:\Windows\System32\GraphicsCapture.dll",
                        "code_id": "65A1B2C31a000",
                    }
                ],
            )
        )
        await session.commit()

    missing_before = (await client.get("/api/v1/symbols/missing")).json()
    gc_missing = next(m for m in missing_before if m["debug_id"] == sync_did)
    assert gc_missing["is_windows_system"] is True
    assert gc_missing["is_available_locally"] is False

    # Wire mock HTTP transport + dump_syms returning exact sync_did for GraphicsCapture.pdb
    fake_pdb = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\x00\x00\x00" + (b"\x00" * 128)
    fake_pe = b"MZ" + (b"\x00" * 128)

    def _msdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/graphicscapture.pdb/{sync_did}/graphicscapture.pdb"):
            return httpx.Response(200, content=fake_pdb)
        if request.url.path.endswith("/graphicscapture.dll/65A1B2C31a000/graphicscapture.dll"):
            return httpx.Response(200, content=fake_pe)
        return httpx.Response(404)

    orig_exec = api_harness.app.state.exec_fn

    async def _sync_exec(*cmd: str, **kwargs: Any) -> Any:
        if "dump_syms" in Path(cmd[0]).name and any(
            "graphicscapture" in str(a).lower() for a in cmd[1:]
        ):
            has_pe = len(cmd) >= 3
            lines = [
                f"MODULE windows x86_64 {sync_did} GraphicsCapture.pdb",
                "INFO CODE_ID 65A1B2C31A000 GraphicsCapture.dll",
                "PUBLIC 1000 0 CaptureStart",
            ]
            if has_pe:
                lines.append("STACK CFI INIT 1000 20 .cfa: $rsp 8 +")
            out = ("\n".join(lines) + "\n").encode()

            class _P:
                returncode = 0

                async def communicate(self) -> tuple[bytes, bytes]:
                    return out, b""

                def kill(self) -> None:
                    return None

            return _P()
        return await orig_exec(*cmd, **kwargs)

    api_harness.app.state.http_transport = httpx.MockTransport(_msdl_handler)
    api_harness.app.state.exec_fn = _sync_exec
    try:
        r_sync = await client.post(
            "/api/v1/symbols/public/sync",
            json={
                "mode": "targets",
                "targets": [
                    {
                        "module_name": "GraphicsCapture.dll",
                        "debug_id": sync_did,
                        "code_id": "65A1B2C31a000",
                    }
                ],
                "force": True,
                "auto_resymbolicate": True,
            },
        )
    finally:
        api_harness.app.state.exec_fn = orig_exec
        api_harness.app.state.http_transport = None

    assert r_sync.status_code == 200
    sync_body = r_sync.json()
    assert sync_body["imported"] == 1
    assert sync_body["results"][0]["status"] == "imported"
    assert sync_body["results"][0]["has_pe_unwind"] is True
    assert sync_body["resymbolicate_queued_count"] == 1
    await _drain(api_harness)

    # Confirm synced symbol appears in public store with source="msdl" and deletes cleanly
    msdl_list = (
        await client.get(
            "/api/v1/symbols", params={"scope": "public", "source": "msdl"}
        )
    ).json()
    assert msdl_list["total"] == 1
    msdl_sym = msdl_list["items"][0]
    assert msdl_sym["debug_id"] == sync_did
    assert msdl_sym["project_id"] is None
    assert msdl_sym["app_version"] is None
    sym_on_disk = api_harness.data_dir / msdl_sym["sym_path"]
    assert sym_on_disk.is_file()

    assert (await client.delete(f"/api/v1/symbols/{msdl_sym['id']}")).status_code == 204
    assert not sym_on_disk.exists()


