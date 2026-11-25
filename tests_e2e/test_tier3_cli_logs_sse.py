from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.tier3


def test_t3_11_project_key_auth_and_rotation_http_and_cli(
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
        json={"key": "t3-11-rot", "name": "Rotate HTTP CLI"},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()
    old_key = proj["api_key"]
    dmp_bytes = fake_toolchain.make_dump()
    dmp_path = tmp_path / "rot.dmp"
    dmp_path.write_bytes(dmp_bytes)

    assert (
        httpx.post(
            f"{live_server}/api/v1/crashes/upload",
            data={"project_key": proj["key"], "app_version": "1.0.0"},
            files={"dump_file": ("rot.dmp", dmp_bytes, "application/octet-stream")},
            timeout=10.0,
        ).status_code
        == 401
    )
    assert (
        httpx.post(
            f"{live_server}/api/v1/crashes/upload",
            data={"project_key": proj["key"], "app_version": "1.0.0"},
            headers={"X-API-Key": "wrong"},
            files={"dump_file": ("rot.dmp", dmp_bytes, "application/octet-stream")},
            timeout=10.0,
        ).status_code
        == 403
    )

    new_key = httpx.post(
        f"{live_server}/api/v1/projects/{proj['id']}/rotate-key",
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()["api_key"]
    assert new_key != old_key

    cli_old = helpers.run_cli(
        [
            "upload-crash",
            "--server",
            live_server,
            "--project",
            proj["key"],
            "--api-key",
            old_key,
            "--dump",
            str(dmp_path),
            "--app-version",
            "1.0.0",
        ],
        env=isolated_env["proc_env"],
    )
    assert cli_old.returncode != 0

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Rotated Key Crash")
    cli_new = helpers.run_cli(
        [
            "upload-crash",
            "--server",
            live_server,
            "--project",
            proj["key"],
            "--api-key",
            new_key,
            "--dump",
            str(dmp_path),
            "--app-version",
            "1.0.0",
            "--wait",
        ],
        env=isolated_env["proc_env"],
    )
    assert cli_new.returncode == 0


async def test_t3_12_cli_upload_symbols_and_crash_sse_dashboard(
    live_server: str,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    import httpx

    async with httpx.AsyncClient(
        base_url=live_server, timeout=20.0, auth=isolated_env["admin_auth"]
    ) as hc:
        proj = (
            await hc.post("/api/v1/projects", json={"key": "t3-12-cli", "name": "CLI SSE Dash"})
        ).json()
        build_dir = tmp_path / "cli_build"
        did = "312011112222333344445555666677771"
        fake_toolchain.make_pe(
            filename="CliApp.exe",
            code_id="C120",
            debug_file="CliApp.pdb",
            debug_id=did,
            write_to=build_dir / "CliApp.exe",
        )
        fake_toolchain.make_pdb(
            filename="CliApp.pdb",
            debug_id=did,
            code_file="CliApp.exe",
            code_id="C120",
            write_to=build_dir / "CliApp.pdb",
        )

        sym_res = await asyncio.to_thread(
            helpers.run_cli,
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
                "2.0.0",
            ],
            isolated_env["proc_env"],
        )
        assert sym_res.returncode == 0

        dmp_path = tmp_path / "cli_app.dmp"
        fake_toolchain.make_dump(
            modules=[
                {
                    "filename": "CliApp.exe",
                    "debug_file": "CliApp.pdb",
                    "debug_id": did,
                    "code_id": "C120",
                    "always_loaded": False,
                }
            ],
            write_to=dmp_path,
        )
        mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="CLI SSE Bug")
        crash_res = await asyncio.to_thread(
            helpers.run_cli,
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
                "--app-version",
                "2.0.0",
                "--wait",
            ],
            isolated_env["proc_env"],
        )
        assert crash_res.returncode == 0
        assert "ANALYZED" in (crash_res.stdout + crash_res.stderr)

        crashes = (await hc.get("/api/v1/crashes", params={"project_id": proj["id"]})).json()[
            "items"
        ]
        assert len(crashes) == 1
        events = await helpers.stream_crash_sse(hc, crashes[0]["id"])
        assert events[-1][1]["status"] == "ANALYZED"

        detail = (await hc.get(f"/api/v1/crashes/{crashes[0]['id']}")).json()
        assert detail["missing_symbols_json"] == []
        assert detail["parsed_stacks_json"]["threads"][0]["frames"][0]["trust"] == "cfi"

        dash = (await hc.get("/api/v1/dashboard", params={"project_id": proj["id"]})).json()
        assert dash["kpis"]["total_crashes"] == 1
        assert dash["kpis"]["analyzed_crashes"] == 1


async def test_t3_13_multilog_upload_log_apis_and_ai_evidence(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-13-multilog")
    app_log = (
        b"2026-11-22 10:00:00.000 INFO boot ok\n"
        b"2026-11-22 10:00:10.000 ERROR Root cause pointer null at 0x0\n"
        b"2026-11-22 10:00:20.000 FATAL Abort after null pointer\n"
    )
    net_log = "2026-11-22 10:00:09.000 WARN 网络连接超时\n".encode("gbk")

    def _handler(req_json: dict[str, Any], headers: dict[str, str], idx: int) -> dict[str, Any]:
        if idx == 0:
            return mock_llm_server._build_completion_payload(
                model=req_json["model"],
                tool_calls=mock_llm_server._make_tool_call_spec([("get_crash_overview", {})])[
                    "tool_calls"
                ],
                finish_reason="tool_calls",
            )
        tool_msgs = [m["content"] for m in req_json["messages"] if m.get("role") == "tool"]
        lid = helpers.extract_log_id(tool_msgs[0], preferred_filename="app.log")
        verdict = mock_llm_server._make_verdict_spec(
            verdict="NEW_ISSUE",
            suggested_title="Null Pointer with Log Evidence",
            evidence_log_snippets=[
                {"log_id": lid, "start_line": 2, "end_line": 3, "relevance": "null ptr"}
            ],
        )
        return mock_llm_server._build_completion_payload(
            model=req_json["model"], content=verdict["content"], finish_reason="stop"
        )

    mock_llm_server.set_handler(_handler)
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("app.log", app_log), ("net.log", net_log)],
        crash_time="2026-11-22T10:00:15Z",
    )
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    assert len(detail["logs"]) == 2
    app_lid, net_lid = up.json()["log_ids"]

    summary = (await client.get(f"/api/v1/logs/{app_lid}/summary")).json()
    assert len(summary["digest"]["recent_errors"]) >= 1

    around = (
        await client.get(
            f"/api/v1/logs/{app_lid}/around-time",
            params={"timestamp": "2026-11-22T10:00:15", "window_sec": 6},
        )
    ).json()
    assert any("Root cause pointer null" in line["text"] for line in around["lines"])

    net_lines = (
        await client.get(f"/api/v1/logs/{net_lid}/lines", params={"start": 1, "end": 5})
    ).json()["lines"]
    assert "网络连接超时" in net_lines[0]["text"]

    snip = detail["ai_runs"][0]["result_json"]["evidence_log_snippets"][0]
    assert snip["log_id"] == app_lid
    ev_lines = (
        await client.get(
            f"/api/v1/logs/{snip['log_id']}/lines",
            params={"start": snip["start_line"], "end": snip["end_line"]},
        )
    ).json()["lines"]
    assert any("Root cause pointer null" in line["text"] for line in ev_lines)


async def test_t3_19_sse_corrupt_dump_vs_late_connect(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t3-19-sse")
    bad_up = await helpers.upload_crash(client, proj["key"], proj["api_key"], b"BAD_DMP_HEADER")
    bad_id = bad_up.json()["crash_id"]
    bad_events = await helpers.stream_crash_sse(client, bad_id)
    assert bad_events[-1][1]["status"] == "FAILED"
    assert bad_events[-1][1]["error"]

    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Good One")
    good_up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump()
    )
    good_id = good_up.json()["crash_id"]
    await helpers.wait_for_crash_status(client, good_id)
    late_events = await helpers.stream_crash_sse(client, good_id, timeout=5.0)
    assert late_events[-1][1]["status"] == "ANALYZED"
    assert late_events[-1][1]["ai_verdict"] == "NEW_ISSUE"


async def test_t3_20_mixed_encoding_log_via_cli_and_log_apis(
    live_server: str,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    import httpx

    async with httpx.AsyncClient(
        base_url=live_server, timeout=20.0, auth=isolated_env["admin_auth"]
    ) as hc:
        proj = (
            await hc.post("/api/v1/projects", json={"key": "t3-20-cli-enc", "name": "CLI Enc"})
        ).json()
        dmp_path = tmp_path / "enc.dmp"
        fake_toolchain.make_dump(write_to=dmp_path)
        log_path = tmp_path / "enc_mixed.log"
        log_path.write_bytes(
            helpers.make_mixed_encoding_log(
                num_lines=40, include_bom=True, include_gbk=True, include_invalid_bytes=True
            )
        )

        mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Mixed Log Crash")
        res = await asyncio.to_thread(
            helpers.run_cli,
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
            isolated_env["proc_env"],
        )
        assert res.returncode == 0

        crashes = (await hc.get("/api/v1/crashes", params={"project_id": proj["id"]})).json()[
            "items"
        ]
        detail = (await hc.get(f"/api/v1/crashes/{crashes[0]['id']}")).json()
        lid = detail["logs"][0]["id"]

        sr = (
            await hc.get(
                f"/api/v1/logs/{lid}/search", params={"pattern": "纹理.*失败", "is_regex": "true"}
            )
        ).json()
        assert sr["total_matches"] >= 1
        assert sr["elapsed_ms"] >= 0
        assert (
            await hc.get(
                f"/api/v1/logs/{lid}/search", params={"pattern": "[unclosed", "is_regex": "true"}
            )
        ).status_code in (400, 422)


def test_t3_24_cli_upload_symbols_pairing_dry_run_and_errors(
    live_server: str,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
    tmp_path: Path,
) -> None:
    import httpx

    proj = httpx.post(
        f"{live_server}/api/v1/projects",
        json={"key": "t3-24-cli", "name": "CLI Sym"},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()
    bdir = tmp_path / "sym_bundle"
    fake_toolchain.make_pe(
        "a.exe", "CA", "a.pdb", "DA1111111111111111111111111111111", write_to=bdir / "a.exe"
    )
    fake_toolchain.make_pdb(
        "a.pdb", "DA1111111111111111111111111111111", "a.exe", "CA", write_to=bdir / "a.pdb"
    )
    fake_toolchain.make_pe(
        "b.dll", "CB", "b.pdb", "DB1111111111111111111111111111111", write_to=bdir / "sub" / "b.dll"
    )
    fake_toolchain.make_pdb(
        "b.pdb", "DB1111111111111111111111111111111", "b.dll", "CB", write_to=bdir / "sub" / "b.pdb"
    )
    fake_toolchain.make_pdb(
        "c.pdb", "DC1111111111111111111111111111111", "c.dll", "CC", write_to=bdir / "c.pdb"
    )
    fake_toolchain.make_pe(
        "unpaired.exe", "CU", "unpaired.pdb", "DU1111", write_to=bdir / "unpaired.exe"
    )

    dry = helpers.run_cli(
        [
            "upload-symbols",
            "--server",
            live_server,
            "--project",
            proj["key"],
            "--api-key",
            proj["api_key"],
            "--dir",
            str(bdir),
            "--app-version",
            "1.0.0",
            "--dry-run",
        ],
        env=isolated_env["proc_env"],
    )
    assert dry.returncode == 0
    snapshots = httpx.get(
        f"{live_server}/api/v1/symbols",
        params={"project_id": proj["id"]},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()
    assert snapshots["total"] == 0

    real = helpers.run_cli(
        [
            "upload-symbols",
            "--server",
            live_server,
            "--project",
            proj["key"],
            "--api-key",
            proj["api_key"],
            "--dir",
            str(bdir),
            "--app-version",
            "1.0.0",
        ],
        env=isolated_env["proc_env"],
    )
    assert real.returncode == 0
    items = httpx.get(
        f"{live_server}/api/v1/symbols",
        params={"project_id": proj["id"]},
        auth=isolated_env["admin_auth"],
        timeout=10.0,
    ).json()["items"]
    assert len(items) == 3
    by_name = {s["debug_file"]: s for s in items}
    assert by_name["a.pdb"]["has_pe_unwind"] is True
    assert by_name["b.pdb"]["has_pe_unwind"] is True
    assert by_name["c.pdb"]["has_pe_unwind"] is False

    bad = helpers.run_cli(
        [
            "upload-symbols",
            "--server",
            live_server,
            "--project",
            proj["key"],
            "--api-key",
            "bad-key",
            "--dir",
            str(bdir),
            "--app-version",
            "1.0.0",
            "--fail-on-error",
        ],
        env=isolated_env["proc_env"],
    )
    assert bad.returncode != 0
