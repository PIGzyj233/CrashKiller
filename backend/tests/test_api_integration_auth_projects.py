"""API integration tests for auth, projects, API keys, settings, quotas, and GC."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests._api_integration_helpers import _drain, open_api_harness


@pytest.fixture
async def api_harness(tmp_path: Path):
    async with open_api_harness(tmp_path) as harness:
        yield harness


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
