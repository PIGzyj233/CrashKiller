from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.tier2


# ---------------------------------------------------------------------------
# Symbol & SymSrv Boundaries (T2-01 .. T2-05)
# ---------------------------------------------------------------------------


async def test_t2_01_symbol_upload_pdb_only(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-pdb-only")
    pdb = fake_toolchain.make_pdb(filename="Solo.pdb", debug_id="100011112222333344445555666677771")
    resp = await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb, "Solo.pdb", pe_bytes=None)
    assert resp.status_code == 201
    body = resp.json()
    assert body["has_pe_unwind"] is False
    assert body["sym_size"] > 0


async def test_t2_02_symbol_upload_pdb_plus_pe(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-pdb-pe")
    did = "200011112222333344445555666677771"
    pdb = fake_toolchain.make_pdb(filename="Full.pdb", debug_id=did, code_file="Full.dll", code_id="C0DE2000")
    pe = fake_toolchain.make_pe(filename="Full.dll", code_id="C0DE2000", debug_file="Full.pdb", debug_id=did)
    resp = await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb, "Full.pdb", pe, "Full.dll")
    assert resp.status_code == 201
    body = resp.json()
    assert body["has_pe_unwind"] is True
    assert body["code_file"] == "Full.dll"
    assert body["code_id"] == "C0DE2000"


async def test_t2_03_symbol_deduplication_and_disk_size(
    client: Any,
    isolated_env: dict[str, Any],
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-sym-dedup")
    did = "300011112222333344445555666677771"
    pdb = fake_toolchain.make_pdb(filename="Dup.pdb", debug_id=did, code_file="Dup.exe", code_id="C0DE3000")
    pe = fake_toolchain.make_pe(filename="Dup.exe", code_id="C0DE3000", debug_file="Dup.pdb", debug_id=did)

    r1 = await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb, "Dup.pdb", pe, "Dup.exe")
    assert r1.status_code == 201
    assert r1.json()["deduplicated"] is False

    sym_root = isolated_env["data_dir"] / "symbols" / "sym"
    sym_bytes_after_1 = sum(p.stat().st_size for p in sym_root.rglob("*.sym"))

    r2 = await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb, "Dup.pdb", pe, "Dup.exe")
    assert r2.status_code == 201
    assert r2.json()["deduplicated"] is True
    assert r2.json()["debug_id"] == did

    sym_bytes_after_2 = sum(p.stat().st_size for p in sym_root.rglob("*.sym"))
    assert sym_bytes_after_2 == sym_bytes_after_1

    listed = (await client.get("/api/v1/symbols", params={"project_id": proj["id"]})).json()
    assert listed["total"] == 1


async def test_t2_04_symbol_upload_corrupt_or_empty_pdb(
    client: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-sym-bad")
    r_bad = await helpers.upload_symbol(
        client, proj["key"], proj["api_key"], pdb_bytes=b"NOT_A_VALID_PDB_GARBAGE", pdb_filename="Bad.pdb"
    )
    assert r_bad.status_code in (400, 422)

    r_empty = await helpers.upload_symbol(
        client, proj["key"], proj["api_key"], pdb_bytes=b"", pdb_filename="Empty.pdb"
    )
    assert r_empty.status_code in (400, 422)

    listed = (await client.get("/api/v1/symbols", params={"project_id": proj["id"]})).json()
    assert listed["total"] == 0


async def test_t2_05_symsrv_archive_raw_false_vs_true(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-symsrv-flag")

    await client.put("/api/v1/settings", json={"archive_raw_symbols": False})
    did1 = "400011112222333344445555666677771"
    pdb1 = fake_toolchain.make_pdb(filename="NoRaw.pdb", debug_id=did1, extra_bytes=b"NORAW")
    assert (await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb1, "NoRaw.pdb")).status_code == 201
    assert (await client.get(f"/symsrv/NoRaw.pdb/{did1}/NoRaw.pdb")).status_code == 404

    await client.put("/api/v1/settings", json={"archive_raw_symbols": True})
    did2 = "400022222222333344445555666677771"
    pdb2 = fake_toolchain.make_pdb(filename="WithRaw.pdb", debug_id=did2, extra_bytes=b"WITHRAW")
    assert (await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb2, "WithRaw.pdb")).status_code == 201
    ok = await client.get(f"/symsrv/WithRaw.pdb/{did2}/WithRaw.pdb")
    assert ok.status_code == 200
    assert ok.content == pdb2

    assert (await client.get("/symsrv/WithRaw.pdb/DOESNOTEXIST/WithRaw.pdb")).status_code == 404


# ---------------------------------------------------------------------------
# Crash Upload Count & Required Field Boundaries (T2-06 .. T2-09)
# ---------------------------------------------------------------------------


async def test_t2_06_crash_upload_zero_logs(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-0-logs")
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Zero Log Crash")
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump(), log_files=None)
    assert up.status_code == 202
    assert up.json()["log_ids"] == []
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    assert detail["status"] == "ANALYZED"
    assert detail["logs"] == []


async def test_t2_07_crash_upload_one_log(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-1-log")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("single.log", helpers.make_mixed_encoding_log(num_lines=10))],
    )
    assert up.status_code == 202
    log_ids = up.json()["log_ids"]
    assert len(log_ids) == 1
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    assert detail["status"] == "ANALYZED"
    assert len(detail["logs"]) == 1
    assert str(detail["logs"][0]["id"]) == log_ids[0]


async def test_t2_08_crash_upload_multiple_logs(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-3-logs")
    mock_llm_server.queue_verdict()
    logs = [
        ("game.log", b"2026-11-22 10:00:01.000 INFO game_marker\n"),
        ("net.log", b"2026-11-22 10:00:02.000 WARN net_marker\n"),
        ("render.log", b"2026-11-22 10:00:03.000 ERROR render_marker\n"),
    ]
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump(), log_files=logs
    )
    assert up.status_code == 202
    log_ids = up.json()["log_ids"]
    assert len(log_ids) == 3
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    seen_texts = []
    for lid in log_ids:
        s = await client.get(f"/api/v1/logs/{lid}/summary")
        assert s.status_code == 200
        lns = await client.get(f"/api/v1/logs/{lid}/lines", params={"start": 1, "end": 5})
        assert lns.status_code == 200
        seen_texts.append(lns.json()["lines"][0]["text"])
    joined = "\n".join(seen_texts)
    assert "game_marker" in joined and "net_marker" in joined and "render_marker" in joined


async def test_t2_09_crash_upload_missing_required_fields(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-missing-fields")
    r_no_dump = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], dump_bytes=None, app_version="1.0.0"
    )
    assert r_no_dump.status_code == 422

    r_no_ver = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], dump_bytes=fake_toolchain.make_dump(), app_version=None
    )
    assert r_no_ver.status_code == 422


# ---------------------------------------------------------------------------
# Log Encoding, Chunking & API Boundaries (T2-10 .. T2-24)
# ---------------------------------------------------------------------------


async def test_t2_10_log_mixed_utf8_bom_gbk_gb18030(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-enc")
    payload = (
        b"\xef\xbb\xbf2026-11-22 10:00:01.000 INFO UTF8-BOM start\n"
        + "2026-11-22 10:00:02.000 ERROR 渲染纹理加载失败 UTF8\n".encode("utf-8")
        + "2026-11-22 10:00:03.000 FATAL 着色器编译异常 GBK\n".encode("gbk")
        + "2026-11-22 10:00:04.000 ERROR 资源句柄无效 𠮷 GB18030\n".encode("gb18030")
    )
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump(), log_files=[("mixed.log", payload)]
    )
    lid = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    lines = (await client.get(f"/api/v1/logs/{lid}/lines", params={"start": 1, "end": 10})).json()["lines"]
    all_text = "\n".join(ln["text"] for ln in lines)
    assert "渲染纹理加载失败" in all_text
    assert "着色器编译异常" in all_text
    assert "资源句柄无效 𠮷 GB18030" in all_text

    sr = (await client.get(f"/api/v1/logs/{lid}/search", params={"pattern": "着色器编译异常", "is_regex": "false"})).json()
    assert sr["total_matches"] == 1
    sr_gb18030 = (await client.get(f"/api/v1/logs/{lid}/search", params={"pattern": "𠮷", "is_regex": "false"})).json()
    assert sr_gb18030["total_matches"] == 1


async def test_t2_11_log_empty_zero_bytes(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-empty-log")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump(), log_files=[("empty.log", b"")]
    )
    lid = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    summary = (await client.get(f"/api/v1/logs/{lid}/summary")).json()
    assert summary["total_lines"] == 0
    lines = await client.get(f"/api/v1/logs/{lid}/lines", params={"start": 1, "end": 10})
    assert lines.status_code == 200
    assert lines.json()["lines"] == []


async def test_t2_12_log_single_line_no_trailing_newline(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-one-line")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("one.log", b"2026-11-22 10:00:00.000 ERROR single line")],
    )
    lid = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    summary = (await client.get(f"/api/v1/logs/{lid}/summary")).json()
    assert summary["total_lines"] == 1
    lines = (await client.get(f"/api/v1/logs/{lid}/lines", params={"start": 1, "end": 1})).json()["lines"]
    assert len(lines) == 1
    assert "single line" in lines[0]["text"]


async def test_t2_13_log_multichunk_boundary_crossing(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-multichunk")
    # Build > 600KB (> 2 * 262144 bytes) log
    line_tpl = "2026-11-22 10:00:00.000 INFO chunk_boundary_probe_payload_line_{idx:06d} " + ("X" * 80) + "\n"
    lines_bytes = "".join(line_tpl.format(idx=i) for i in range(1, 5001)).encode("utf-8")
    assert len(lines_bytes) > 600_000

    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump(), log_files=[("big.log", lines_bytes)]
    )
    lid = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    summary = (await client.get(f"/api/v1/logs/{lid}/summary")).json()
    assert summary["total_lines"] == 5000
    assert summary["compressed_size"] < summary["raw_size"]

    # Read a range guaranteed to cross the 256KB chunk 0 -> chunk 1 boundary (~line 2000..2200)
    page = (await client.get(f"/api/v1/logs/{lid}/lines", params={"start": 1950, "end": 2149})).json()
    assert len(page["lines"]) == 200
    for offset, item in enumerate(page["lines"]):
        expected_no = 1950 + offset
        assert item["line_number"] == expected_no
        assert f"line_{expected_no:06d}" in item["text"]


async def test_t2_14_log_lines_start_1_end_1(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-l-1-1")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("l.log", helpers.make_mixed_encoding_log(num_lines=20))],
    )
    lid = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    r = await client.get(f"/api/v1/logs/{lid}/lines", params={"start": 1, "end": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["start_line"] == 1 and body["end_line"] == 1
    assert len(body["lines"]) == 1
    assert body["lines"][0]["line_number"] == 1


async def test_t2_15_log_lines_start_gt_end_422(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-l-sgt")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("l.log", helpers.make_mixed_encoding_log(num_lines=60))],
    )
    lid = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    r = await client.get(f"/api/v1/logs/{lid}/lines", params={"start": 50, "end": 10})
    assert r.status_code == 422


async def test_t2_16_log_lines_non_positive_start_422(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-l-zero")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("l.log", helpers.make_mixed_encoding_log(num_lines=20))],
    )
    lid = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    assert (await client.get(f"/api/v1/logs/{lid}/lines", params={"start": 0, "end": 10})).status_code == 422
    assert (await client.get(f"/api/v1/logs/{lid}/lines", params={"start": -5, "end": 5})).status_code == 422


async def test_t2_17_log_lines_end_gt_total_clamped(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-l-clamp")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("l.log", helpers.make_mixed_encoding_log(num_lines=20))],
    )
    lid = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    r = await client.get(f"/api/v1/logs/{lid}/lines", params={"start": 1, "end": 999999})
    assert r.status_code == 200
    body = r.json()
    assert body["end_line"] == 20
    assert len(body["lines"]) == 20


async def test_t2_18_log_lines_start_gt_total_empty(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-l-oob")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("l.log", helpers.make_mixed_encoding_log(num_lines=20))],
    )
    lid = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    r = await client.get(f"/api/v1/logs/{lid}/lines", params={"start": 5000, "end": 5010})
    assert r.status_code == 200
    assert r.json()["lines"] == []


async def test_t2_19_log_around_time_invalid_timestamp(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-bad-ts")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("l.log", helpers.make_mixed_encoding_log(num_lines=10))],
    )
    lid = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    r = await client.get(f"/api/v1/logs/{lid}/around-time", params={"timestamp": "not-a-date", "window_sec": 10})
    assert r.status_code in (400, 422)


async def test_t2_20_log_around_time_without_timestamps(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-no-ts")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("nots.log", b"plain line one\nplain line two\nplain line three\n")],
    )
    lid = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    r = await client.get(
        f"/api/v1/logs/{lid}/around-time",
        params={"timestamp": "2026-11-22T10:00:00Z", "window_sec": 10},
    )
    assert r.status_code == 200
    assert r.json()["lines"] == []


async def test_t2_21_log_search_invalid_regex_422(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-bad-re")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("l.log", helpers.make_mixed_encoding_log(num_lines=10))],
    )
    lid = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    r = await client.get(f"/api/v1/logs/{lid}/search", params={"pattern": "[unclosed(", "is_regex": "true"})
    assert r.status_code in (400, 422)


async def test_t2_22_log_search_max_matches_truncation(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-trunc")
    content = "".join(f"2026-11-22 10:00:00.000 ERROR match_target_{i}\n" for i in range(50)).encode("utf-8")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump(), log_files=[("m.log", content)]
    )
    lid = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    r = await client.get(
        f"/api/v1/logs/{lid}/search",
        params={"pattern": "match_target", "is_regex": "false", "max_matches": 5},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["matches"]) == 5
    assert body["truncated"] is True
    assert body["total_matches"] >= 5


async def test_t2_23_log_search_case_sensitivity(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-case")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("c.log", b"2026-11-22 10:00:00.000 ERROR fatalerror happened\n")],
    )
    lid = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    r_sens = await client.get(
        f"/api/v1/logs/{lid}/search",
        params={"pattern": "FatalError", "is_regex": "false", "case_sensitive": "true"},
    )
    assert r_sens.status_code == 200
    assert r_sens.json()["matches"] == []

    r_ins = await client.get(
        f"/api/v1/logs/{lid}/search",
        params={"pattern": "FatalError", "is_regex": "false", "case_sensitive": "false"},
    )
    assert r_ins.status_code == 200
    assert len(r_ins.json()["matches"]) == 1


async def test_t2_24_log_search_literal_metacharacters(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-lit-meta")
    content = b"2026-11-22 10:00:00.000 INFO arr0.ptr false positive\n2026-11-22 10:00:01.000 ERROR arr[0].ptr null\n"
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump(), log_files=[("lit.log", content)]
    )
    lid = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    r = await client.get(
        f"/api/v1/logs/{lid}/search",
        params={"pattern": "arr[0].ptr", "is_regex": "false"},
    )
    assert r.status_code == 200
    matches = r.json()["matches"]
    assert len(matches) == 1
    assert matches[0]["line_number"] == 2


# ---------------------------------------------------------------------------
# AI Agent Guardrails & Triage Modes (T2-25 .. T2-32)
# ---------------------------------------------------------------------------


async def test_t2_25_ai_max_steps_exceeded_needs_review(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    await client.put("/api/v1/settings", json={"ai_max_steps": 2, "ai_triage_mode": "ALWAYS"})
    mock_llm_server.set_loop_forever("get_crash_overview", {})

    proj = await helpers.create_project(client, key="t2-max-steps")
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump())
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    assert detail["status"] == "ANALYZED"
    assert detail["ai_verdict"] == "NEEDS_REVIEW"
    assert len(detail["ai_runs"]) == 1
    assert detail["ai_runs"][0]["result_json"]["verdict"] == "NEEDS_REVIEW"
    assert len(detail["ai_runs"][0]["trace_json"]) <= 3


async def test_t2_26_ai_timeout_exceeded_needs_review(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    await client.put("/api/v1/settings", json={"ai_timeout_seconds": 1, "ai_triage_mode": "ALWAYS"})
    mock_llm_server.set_delay(2.5)

    proj = await helpers.create_project(client, key="t2-timeout")
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump())
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"], timeout=15.0)

    assert detail["status"] == "ANALYZED"
    assert detail["ai_verdict"] == "NEEDS_REVIEW"
    assert len(detail["ai_runs"]) >= 1


async def test_t2_27_ai_missing_api_key_needs_review(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    await client.put("/api/v1/settings", json={"llm_api_key": "", "ai_triage_mode": "ALWAYS"})
    proj = await helpers.create_project(client, key="t2-nokey")
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump())
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    assert detail["status"] == "ANALYZED"
    assert detail["ai_verdict"] == "NEEDS_REVIEW"


async def test_t2_28_ai_tool_output_max_chars_truncation(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    await client.put("/api/v1/settings", json={"ai_tool_output_max_chars": 500, "ai_triage_mode": "ALWAYS"})
    proj = await helpers.create_project(client, key="t2-trunc-tool")
    verbose_log = "".join(f"2026-11-22 10:00:00.000 INFO verbose_line_{i}_" + ("A" * 120) + "\n" for i in range(40)).encode("utf-8")

    expected_log_id: list[str] = []

    def _handler(req_json: dict[str, Any], headers: dict[str, str], idx: int) -> dict[str, Any]:
        if idx == 0:
            return mock_llm_server._build_completion_payload(
                model=req_json["model"],
                tool_calls=mock_llm_server._make_tool_call_spec([("get_crash_overview", {})])["tool_calls"],
                finish_reason="tool_calls",
            )
        if idx == 1:
            tool_msgs = [m["content"] for m in req_json["messages"] if m.get("role") == "tool"]
            lid = helpers.extract_log_id(tool_msgs[0]) or (expected_log_id[0] if expected_log_id else "")
            return mock_llm_server._build_completion_payload(
                model=req_json["model"],
                tool_calls=mock_llm_server._make_tool_call_spec(
                    [("read_log_lines", {"log_id": lid, "start_line": 1, "end_line": 40})]
                )["tool_calls"],
                finish_reason="tool_calls",
            )
        verdict = mock_llm_server._make_verdict_spec(verdict="NEW_ISSUE", suggested_title="Truncated Tool")
        return mock_llm_server._build_completion_payload(
            model=req_json["model"], content=verdict["content"], finish_reason="stop"
        )

    mock_llm_server.set_handler(_handler)
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump(), log_files=[("v.log", verbose_log)]
    )
    expected_log_id.append(up.json()["log_ids"][0])
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    assert detail["status"] == "ANALYZED"

    # Verify the tool message sent back to LLM on turn 3 was real log content truncated <= 600 chars
    third_req_msgs = mock_llm_server.requests[2]["body"]["messages"]
    tool_msgs = [m for m in third_req_msgs if m.get("role") == "tool"]
    tool_content = tool_msgs[-1]["content"]
    assert "verbose_line_" in tool_content, f"Expected real log content, got: {tool_content!r}"
    assert "Log not found" not in tool_content
    assert 250 <= len(tool_content) <= 600


async def test_t2_29_ai_triage_mode_manual_skips_ai(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    await client.put("/api/v1/settings", json={"ai_triage_mode": "MANUAL"})
    proj = await helpers.create_project(client, key="t2-manual")
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump())
    detail = await helpers.wait_for_crash_status(client, up.json()["crash_id"])
    assert detail["status"] == "ANALYZED"
    assert detail["ai_runs"] == []
    assert detail["ai_verdict"] is None
    assert len(mock_llm_server.requests) == 0


async def test_t2_30_smart_triage_unambiguous_known_cluster_skips_ai(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-smart-known")
    dmp = fake_toolchain.make_dump()
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Unambiguous Bug")
    c1 = await helpers.wait_for_crash_status(
        client, (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)).json()["crash_id"]
    )
    await client.patch(f"/api/v1/issues/{c1['issue']['id']}", json={"is_ambiguous_stack": False})
    mock_llm_server.reset()

    c2 = await helpers.wait_for_crash_status(
        client, (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)).json()["crash_id"]
    )
    assert c2["status"] == "ANALYZED"
    assert c2["issue"]["id"] == c1["issue"]["id"]
    assert c2["ai_runs"] == []
    assert len(mock_llm_server.requests) == 0


async def test_t2_31_smart_triage_ambiguous_stack_triggers_ai(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-smart-ambig")
    dmp = fake_toolchain.make_dump()
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Ambiguous Assert")
    c1 = await helpers.wait_for_crash_status(
        client, (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)).json()["crash_id"]
    )
    await client.patch(f"/api/v1/issues/{c1['issue']['id']}", json={"is_ambiguous_stack": True})
    mock_llm_server.reset()

    mock_llm_server.queue_verdict(verdict="KNOWN_ISSUE", matched_issue_id=c1["issue"]["id"])
    c2 = await helpers.wait_for_crash_status(
        client, (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp)).json()["crash_id"]
    )
    assert c2["status"] == "ANALYZED"
    assert len(c2["ai_runs"]) == 1
    assert len(mock_llm_server.requests) >= 1


async def test_t2_32_regression_detection_after_fixed_version(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-regression")
    dmp = fake_toolchain.make_dump()
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Shader Crash")
    c1 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp, app_version="1.1.0")).json()["crash_id"],
    )
    issue_id = c1["issue"]["id"]
    await client.patch(
        f"/api/v1/issues/{issue_id}",
        json={"status": "FIXED", "fixed_in_version": "1.2.0", "is_ambiguous_stack": True},
    )

    mock_llm_server.queue_verdict(verdict="REGRESSION", matched_issue_id=issue_id)
    c2 = await helpers.wait_for_crash_status(
        client,
        (await helpers.upload_crash(client, proj["key"], proj["api_key"], dmp, app_version="1.2.5")).json()["crash_id"],
    )
    assert c2["ai_verdict"] == "REGRESSION"


# ---------------------------------------------------------------------------
# Storage, Safety Floor, Pinning & Post-GC Persistence (T2-33 .. T2-36)
# ---------------------------------------------------------------------------


async def test_t2_33_host_safety_floor_breached(
    client: Any,
    fake_toolchain: Any,
    fake_disk_usage: Any,
    helpers: Any,
) -> None:
    fake_disk_usage.set_free_gb(40.0)
    await client.put("/api/v1/settings", json={"archive_raw_symbols": True, "host_safety_floor_gb": 50.0})

    proj = await helpers.create_project(client, key="t2-floor")
    did = "500011112222333344445555666677771"
    pdb = fake_toolchain.make_pdb(filename="Floor.pdb", debug_id=did)
    sym_resp = await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb, "Floor.pdb")
    assert sym_resp.status_code == 201
    assert sym_resp.json()["raw_size"] == 0
    assert (await client.get(f"/symsrv/Floor.pdb/{did}/Floor.pdb")).status_code == 404

    dash = (await client.get("/api/v1/dashboard")).json()
    assert dash["storage"]["safety_floor_breached"] is True

    gc_resp = await client.post("/api/v1/settings/gc", json={"force_emergency": False})
    assert gc_resp.status_code == 200
    assert gc_resp.json()["emergency_triggered"] is True


async def test_t2_34_is_pinned_crash_protection(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-pin-crash")
    mock_llm_server.queue_verdict()
    c_pinned = (
        await helpers.upload_crash(
            client,
            proj["key"],
            proj["api_key"],
            fake_toolchain.make_dump(extra_bytes=b"PINNED"),
            log_files=[("pin.log", helpers.make_mixed_encoding_log(num_lines=20))],
        )
    ).json()["crash_id"]
    await helpers.wait_for_crash_status(client, c_pinned)
    await client.patch(f"/api/v1/crashes/{c_pinned}/pin", json={"is_pinned": True})

    mock_llm_server.queue_verdict()
    c_unpinned = (
        await helpers.upload_crash(
            client,
            proj["key"],
            proj["api_key"],
            fake_toolchain.make_dump(extra_bytes=b"UNPINNED"),
            log_files=[("unpin.log", helpers.make_mixed_encoding_log(num_lines=20))],
        )
    ).json()["crash_id"]
    await helpers.wait_for_crash_status(client, c_unpinned)

    gc = (await client.post("/api/v1/settings/gc", json={"force_emergency": True})).json()
    assert gc["skipped_pinned_count"] >= 1
    assert (await client.get(f"/api/v1/crashes/{c_pinned}/artifacts/dump")).status_code == 200
    assert (await client.get(f"/api/v1/crashes/{c_unpinned}/artifacts/dump")).status_code in (404, 410)


async def test_t2_35_is_pinned_issue_protection(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-pin-issue")
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Pinned Issue")
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(extra_bytes=b"ISSUE_PINNED"),
        log_files=[("ip.log", helpers.make_mixed_encoding_log(num_lines=15))],
    )
    crash_id = up.json()["crash_id"]
    detail = await helpers.wait_for_crash_status(client, crash_id)
    issue_id = detail["issue"]["id"]

    await client.patch(f"/api/v1/issues/{issue_id}", json={"is_pinned": True})

    gc = (await client.post("/api/v1/settings/gc", json={"force_emergency": True})).json()
    assert gc["skipped_pinned_count"] >= 1
    assert (await client.get(f"/api/v1/crashes/{crash_id}/artifacts/dump")).status_code == 200


async def test_t2_36_post_gc_artifact_and_db_persistence(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-post-gc")
    mock_llm_server.queue_verdict(verdict="NEW_ISSUE", suggested_title="Persisted Report")
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("gc.log", helpers.make_mixed_encoding_log(num_lines=20))],
    )
    crash_id = up.json()["crash_id"]
    log_id = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, crash_id)

    await client.post("/api/v1/settings/gc", json={"force_emergency": True})

    assert (await client.get(f"/api/v1/crashes/{crash_id}/artifacts/dump")).status_code in (404, 410)
    assert (await client.get(f"/api/v1/logs/{log_id}/lines", params={"start": 1, "end": 5})).status_code in (404, 410)
    assert (await client.post(f"/api/v1/crashes/{crash_id}/reprocess", json={"mode": "full"})).status_code == 409

    detail_resp = await client.get(f"/api/v1/crashes/{crash_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["parsed_stacks_json"] is not None
    assert len(detail["ai_runs"]) >= 1


# ---------------------------------------------------------------------------
# Validation, Auth, Issues & 404 Boundaries (T2-37 .. T2-47)
# ---------------------------------------------------------------------------


async def test_t2_37_settings_validation_negative_and_invalid_mode(client: Any) -> None:
    assert (await client.put("/api/v1/settings", json={"host_safety_floor_gb": -1.0})).status_code == 422
    assert (await client.put("/api/v1/settings", json={"total_disk_quota_gb": -5.0})).status_code == 422
    assert (await client.put("/api/v1/settings", json={"ai_triage_mode": "INVALID_MODE"})).status_code == 422


async def test_t2_38_auth_missing_api_key_401(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-auth-401")
    r1 = await helpers.upload_crash(client, proj["key"], api_key=None, dump_bytes=fake_toolchain.make_dump())
    assert r1.status_code == 401
    r2 = await helpers.upload_symbol(client, proj["key"], api_key=None, pdb_bytes=fake_toolchain.make_pdb())
    assert r2.status_code == 401


async def test_t2_39_auth_wrong_api_key_403(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-auth-403")
    r1 = await helpers.upload_crash(client, proj["key"], api_key="bogus-key", dump_bytes=fake_toolchain.make_dump())
    assert r1.status_code == 403
    r2 = await helpers.upload_symbol(client, proj["key"], api_key="bogus-key", pdb_bytes=fake_toolchain.make_pdb())
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

    r_old = await helpers.upload_crash(client, proj["key"], api_key=old_key, dump_bytes=fake_toolchain.make_dump())
    assert r_old.status_code == 403

    mock_llm_server.queue_verdict()
    r_new = await helpers.upload_crash(client, proj["key"], api_key=new_key, dump_bytes=fake_toolchain.make_dump())
    assert r_new.status_code == 202
    await helpers.wait_for_crash_status(client, r_new.json()["crash_id"])


async def test_t2_41_upload_unknown_project_404(
    client: Any,
    fake_toolchain: Any,
    helpers: Any,
) -> None:
    r1 = await helpers.upload_crash(client, "nonexistent-proj", api_key="any", dump_bytes=fake_toolchain.make_dump())
    assert r1.status_code == 404
    r2 = await helpers.upload_symbol(client, "nonexistent-proj", api_key="any", pdb_bytes=fake_toolchain.make_pdb())
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
        await client.post("/api/v1/issues", json={"project_id": proj["id"], "title": "Bad", "status": "BOGUS"})
    ).status_code == 422


async def test_t2_44_issues_cross_project_merge_400(client: Any, helpers: Any) -> None:
    p1 = await helpers.create_project(client, key="t2-m-p1")
    p2 = await helpers.create_project(client, key="t2-m-p2")
    i1 = (await client.post("/api/v1/issues", json={"project_id": p1["id"], "title": "I1"})).json()
    i2 = (await client.post("/api/v1/issues", json={"project_id": p2["id"], "title": "I2"})).json()

    resp = await client.post("/api/v1/issues/merge", json={"target_issue_id": i1["id"], "source_issue_ids": [i2["id"]]})
    assert resp.status_code == 400


async def test_t2_45_issues_self_merge_400(client: Any, helpers: Any) -> None:
    p1 = await helpers.create_project(client, key="t2-self-m")
    i1 = (await client.post("/api/v1/issues", json={"project_id": p1["id"], "title": "Self"})).json()

    resp = await client.post("/api/v1/issues/merge", json={"target_issue_id": i1["id"], "source_issue_ids": [i1["id"]]})
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
        client, (await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump())).json()["crash_id"]
    )
    i_other = (await client.post("/api/v1/issues", json={"project_id": proj["id"], "title": "Other"})).json()

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
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], fake_toolchain.make_dump())
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
    up = await helpers.upload_crash(client, proj["key"], proj["api_key"], dump_bytes=b"CORRUPT_MINIDUMP_BYTES")
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
            "--server", live_server,
            "--project", proj["key"],
            "--api-key", proj["api_key"],
            "--dump", str(tmp_path / "does_not_exist.dmp"),
            "--app-version", "1.0.0",
        ],
        env=isolated_env["proc_env"],
    )
    assert r_missing.returncode != 0

    dmp_path = tmp_path / "valid.dmp"
    fake_toolchain.make_dump(write_to=dmp_path)
    r_bad_key = helpers.run_cli(
        [
            "upload-crash",
            "--server", live_server,
            "--project", proj["key"],
            "--api-key", "wrong-secret-key",
            "--dump", str(dmp_path),
            "--app-version", "1.0.0",
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
            "--server", live_server,
            "--project", proj["key"],
            "--api-key", "wrong-api-key",
            "--dir", str(build_dir),
            "--app-version", "1.0.0",
            "--fail-on-error",
        ],
        env=isolated_env["proc_env"],
    )
    assert res.returncode != 0
