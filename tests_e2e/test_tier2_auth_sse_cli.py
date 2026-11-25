from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.tier2


# ---------------------------------------------------------------------------
# Validation, Auth, Issues & 404 Boundaries (T2-37 .. T2-47)
# ---------------------------------------------------------------------------


async def test_t2_37_settings_validation_negative_and_invalid_mode(client: Any) -> None:
    assert (
        await client.put("/api/v1/settings", json={"host_safety_floor_gb": -1.0})
    ).status_code == 422
    assert (
        await client.put("/api/v1/settings", json={"total_disk_quota_gb": -5.0})
    ).status_code == 422
    assert (
        await client.put("/api/v1/settings", json={"ai_triage_mode": "INVALID_MODE"})
    ).status_code == 422


async def test_t2_38_auth_missing_api_key_401(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-auth-401")
    r1 = await helpers.upload_crash(
        client, proj["key"], api_key=None, dump_bytes=fake_toolchain.make_dump()
    )
    assert r1.status_code == 401
    r2 = await helpers.upload_symbol(
        client, proj["key"], api_key=None, pdb_bytes=fake_toolchain.make_pdb()
    )
    assert r2.status_code == 401


async def test_t2_39_auth_wrong_api_key_403(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-auth-403")
    r1 = await helpers.upload_crash(
        client, proj["key"], api_key="bogus-key", dump_bytes=fake_toolchain.make_dump()
    )
    assert r1.status_code == 403
    r2 = await helpers.upload_symbol(
        client, proj["key"], api_key="bogus-key", pdb_bytes=fake_toolchain.make_pdb()
    )
    assert r2.status_code == 403


async def test_t2_40_auth_rotated_old_key_rejected(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-auth-rot")
    old_key = proj["api_key"]
    rot = await client.post(f"/api/v1/projects/{proj['id']}/rotate-key")
    new_key = rot.json()["api_key"]

    r_old = await helpers.upload_crash(
        client, proj["key"], api_key=old_key, dump_bytes=fake_toolchain.make_dump()
    )
    assert r_old.status_code == 403

    mock_llm_server.queue_verdict()
    r_new = await helpers.upload_crash(
        client, proj["key"], api_key=new_key, dump_bytes=fake_toolchain.make_dump()
    )
    assert r_new.status_code == 202
    await helpers.wait_for_crash_status(client, r_new.json()["crash_id"])


async def test_t2_41_upload_unknown_project_404(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    r1 = await helpers.upload_crash(
        client, "nonexistent-proj", api_key="any", dump_bytes=fake_toolchain.make_dump()
    )
    assert r1.status_code == 404
    r2 = await helpers.upload_symbol(
        client, "nonexistent-proj", api_key="any", pdb_bytes=fake_toolchain.make_pdb()
    )
    assert r2.status_code == 404


async def test_t2_42_projects_duplicate_key_409(client: Any) -> None:
    r1 = await client.post("/api/v1/projects", json={"key": "dup-key", "name": "First"})
    assert r1.status_code == 201
    r2 = await client.post("/api/v1/projects", json={"key": "dup-key", "name": "Second"})
    assert r2.status_code == 409


async def test_t2_43_invalid_status_enums_422(client: Any, helpers: Any) -> None:
    proj = await helpers.create_project(client, key="t2-enums")
    assert (await client.get("/api/v1/crashes", params={"status": "BOGUS"})).status_code == 422
    assert (await client.get("/api/v1/issues", params={"status": "BOGUS"})).status_code == 422
    assert (
        await client.post(
            "/api/v1/issues", json={"project_id": proj["id"], "title": "Bad", "status": "BOGUS"}
        )
    ).status_code == 422


async def test_t2_44_issues_cross_project_merge_400(client: Any, helpers: Any) -> None:
    p1 = await helpers.create_project(client, key="t2-m-p1")
    p2 = await helpers.create_project(client, key="t2-m-p2")
    i1 = (await client.post("/api/v1/issues", json={"project_id": p1["id"], "title": "I1"})).json()
    i2 = (await client.post("/api/v1/issues", json={"project_id": p2["id"], "title": "I2"})).json()

    resp = await client.post(
        "/api/v1/issues/merge", json={"target_issue_id": i1["id"], "source_issue_ids": [i2["id"]]}
    )
    assert resp.status_code == 400


async def test_t2_45_issues_self_merge_400(client: Any, helpers: Any) -> None:
    p1 = await helpers.create_project(client, key="t2-self-m")
    i1 = (
        await client.post("/api/v1/issues", json={"project_id": p1["id"], "title": "Self"})
    ).json()

    resp = await client.post(
        "/api/v1/issues/merge", json={"target_issue_id": i1["id"], "source_issue_ids": [i1["id"]]}
    )
    assert resp.status_code == 400


async def test_t2_46_issues_split_foreign_cluster_400(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-split-bad")
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="I1")
    c1 = await helpers.wait_for_crash_status(
        client,
        (
            await helpers.upload_crash(
                client, proj["key"], proj["api_key"], fake_toolchain.make_dump()
            )
        ).json()["crash_id"],
    )
    i_other = (
        await client.post("/api/v1/issues", json={"project_id": proj["id"], "title": "Other"})
    ).json()

    resp = await client.post(
        f"/api/v1/issues/{i_other['id']}/split",
        json={"cluster_id": c1["cluster"]["id"], "new_issue_title": "Should Fail"},
    )
    assert resp.status_code == 400


async def test_t2_47_resource_not_found_404(client: Any) -> None:
    rand_id = str(uuid.uuid4())
    assert (await client.get(f"/api/v1/crashes/{rand_id}")).status_code == 404
    assert (await client.get("/api/v1/issues/999999")).status_code == 404
    assert (await client.delete(f"/api/v1/symbols/{rand_id}")).status_code == 404
    assert (await client.get(f"/api/v1/logs/{rand_id}/summary")).status_code == 404
    assert (await client.get(f"/api/v1/projects/{rand_id}")).status_code == 404


# ---------------------------------------------------------------------------
# SSE & CLI Error Boundaries (T2-48 .. T2-52)
# ---------------------------------------------------------------------------


async def test_t2_48_sse_late_connect_analyzed_crash(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-sse-late")
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Late SSE")
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump()
    )
    crash_id = up.json()["crash_id"]
    await helpers.wait_for_crash_status(client, crash_id)

    events = await helpers.stream_crash_sse(client, crash_id, timeout=5.0)
    assert len(events) >= 1
    assert events[-1][1]["status"] == "ANALYZED"
    assert events[-1][1]["ai_verdict"] == "NEW_ISSUE"


async def test_t2_49_sse_late_connect_failed_corrupt_dump(
    client: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-sse-fail")
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], dump_bytes=b"CORRUPT_MINIDUMP_BYTES"
    )
    assert up.status_code == 202
    crash_id = up.json()["crash_id"]
    detail = await helpers.wait_for_crash_status(client, crash_id, target_statuses=("FAILED",))
    assert detail["status"] == "FAILED"

    events = await helpers.stream_crash_sse(client, crash_id, timeout=5.0)
    assert len(events) >= 1
    assert events[-1][1]["status"] == "FAILED"
    assert events[-1][1].get("error")


async def test_t2_50_sse_unknown_crash_404(client: Any) -> None:
    resp = await client.get(f"/api/v1/crashes/{uuid.uuid4()}/events")
    assert resp.status_code == 404


def test_t2_51_cli_missing_dump_and_bad_api_key(
    live_server: str,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    import httpx

    proj = httpx.post(
        f"{live_server}/api/v1/projects",
        json={"key": "cli-bad", "name": "CLI Bad"},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()

    r_missing = helpers.run_cli(
        [
            "upload-crash",
            "--server",
            live_server,
            "--project",
            proj["key"],
            "--api-key",
            proj["api_key"],
            "--dump",
            str(tmp_path / "does_not_exist.dmp"),
            "--app-version",
            "1.0.0",
        ],
        env=isolated_env["proc_env"],
    )
    assert r_missing.returncode != 0

    dmp_path = tmp_path / "valid.dmp"
    fake_toolchain.make_dump(write_to=dmp_path)
    r_bad_key = helpers.run_cli(
        [
            "upload-crash",
            "--server",
            live_server,
            "--project",
            proj["key"],
            "--api-key",
            "wrong-secret-key",
            "--dump",
            str(dmp_path),
            "--app-version",
            "1.0.0",
        ],
        env=isolated_env["proc_env"],
    )
    assert r_bad_key.returncode != 0


def test_t2_52_cli_upload_symbols_fail_on_error(
    live_server: str,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    import httpx

    proj = httpx.post(
        f"{live_server}/api/v1/projects",
        json={"key": "cli-foe", "name": "CLI FOE"},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()

    build_dir = tmp_path / "foe_build"
    fake_toolchain.make_pdb(write_to=build_dir / "Foe.pdb")

    res = helpers.run_cli(
        [
            "upload-symbols",
            "--server",
            live_server,
            "--project",
            proj["key"],
            "--api-key",
            "wrong-api-key",
            "--dir",
            str(build_dir),
            "--app-version",
            "1.0.0",
            "--fail-on-error",
        ],
        env=isolated_env["proc_env"],
    )
    assert res.returncode != 0
