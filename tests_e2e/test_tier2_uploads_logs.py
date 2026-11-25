from __future__ import annotations

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
    resp = await helpers.upload_symbol(
        client, proj["key"], proj["api_key"], pdb, "Solo.pdb", pe_bytes=None
    )
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
    pdb = fake_toolchain.make_pdb(
        filename="Full.pdb", debug_id=did, code_file="Full.dll", code_id="C0DE2000"
    )
    pe = fake_toolchain.make_pe(
        filename="Full.dll", code_id="C0DE2000", debug_file="Full.pdb", debug_id=did
    )
    resp = await helpers.upload_symbol(
        client, proj["key"], proj["api_key"], pdb, "Full.pdb", pe, "Full.dll"
    )
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
    pdb = fake_toolchain.make_pdb(
        filename="Dup.pdb", debug_id=did, code_file="Dup.exe", code_id="C0DE3000"
    )
    pe = fake_toolchain.make_pe(
        filename="Dup.exe", code_id="C0DE3000", debug_file="Dup.pdb", debug_id=did
    )

    r1 = await helpers.upload_symbol(
        client, proj["key"], proj["api_key"], pdb, "Dup.pdb", pe, "Dup.exe"
    )
    assert r1.status_code == 201
    assert r1.json()["deduplicated"] is False

    sym_root = isolated_env["data_dir"] / "symbols" / "sym"
    sym_bytes_after_1 = sum(p.stat().st_size for p in sym_root.rglob("*.sym"))

    r2 = await helpers.upload_symbol(
        client, proj["key"], proj["api_key"], pdb, "Dup.pdb", pe, "Dup.exe"
    )
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
        client,
        proj["key"],
        proj["api_key"],
        pdb_bytes=b"NOT_A_VALID_PDB_GARBAGE",
        pdb_filename="Bad.pdb",
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
    assert (
        await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb1, "NoRaw.pdb")
    ).status_code == 201
    assert (await client.get(f"/symsrv/NoRaw.pdb/{did1}/NoRaw.pdb")).status_code == 404

    await client.put("/api/v1/settings", json={"archive_raw_symbols": True})
    did2 = "400022222222333344445555666677771"
    pdb2 = fake_toolchain.make_pdb(filename="WithRaw.pdb", debug_id=did2, extra_bytes=b"WITHRAW")
    assert (
        await helpers.upload_symbol(client, proj["key"], proj["api_key"], pdb2, "WithRaw.pdb")
    ).status_code == 201
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
    up = await helpers.upload_crash(
        client, proj["key"], proj["api_key"], fake_toolchain.make_dump(), log_files=None
    )
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
        client,
        proj["key"],
        proj["api_key"],
        dump_bytes=fake_toolchain.make_dump(),
        app_version=None,
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
        + "2026-11-22 10:00:02.000 ERROR 渲染纹理加载失败 UTF8\n".encode()
        + "2026-11-22 10:00:03.000 FATAL 着色器编译异常 GBK\n".encode("gbk")
        + "2026-11-22 10:00:04.000 ERROR 资源句柄无效 𠮷 GB18030\n".encode("gb18030")
    )
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("mixed.log", payload)],
    )
    lid = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    lines = (await client.get(f"/api/v1/logs/{lid}/lines", params={"start": 1, "end": 10})).json()[
        "lines"
    ]
    all_text = "\n".join(ln["text"] for ln in lines)
    assert "渲染纹理加载失败" in all_text
    assert "着色器编译异常" in all_text
    assert "资源句柄无效 𠮷 GB18030" in all_text

    sr = (
        await client.get(
            f"/api/v1/logs/{lid}/search", params={"pattern": "着色器编译异常", "is_regex": "false"}
        )
    ).json()
    assert sr["total_matches"] == 1
    sr_gb18030 = (
        await client.get(
            f"/api/v1/logs/{lid}/search", params={"pattern": "𠮷", "is_regex": "false"}
        )
    ).json()
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
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("empty.log", b"")],
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
    lines = (await client.get(f"/api/v1/logs/{lid}/lines", params={"start": 1, "end": 1})).json()[
        "lines"
    ]
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
    line_tpl = (
        "2026-11-22 10:00:00.000 INFO chunk_boundary_probe_payload_line_{idx:06d} "
        + ("X" * 80)
        + "\n"
    )
    lines_bytes = "".join(line_tpl.format(idx=i) for i in range(1, 5001)).encode("utf-8")
    assert len(lines_bytes) > 600_000

    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("big.log", lines_bytes)],
    )
    lid = up.json()["log_ids"][0]
    await helpers.wait_for_crash_status(client, up.json()["crash_id"])

    summary = (await client.get(f"/api/v1/logs/{lid}/summary")).json()
    assert summary["total_lines"] == 5000
    assert summary["compressed_size"] < summary["raw_size"]

    # Read a range guaranteed to cross the 256KB chunk 0 -> chunk 1 boundary (~line 2000..2200)
    page = (
        await client.get(f"/api/v1/logs/{lid}/lines", params={"start": 1950, "end": 2149})
    ).json()
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

    assert (
        await client.get(f"/api/v1/logs/{lid}/lines", params={"start": 0, "end": 10})
    ).status_code == 422
    assert (
        await client.get(f"/api/v1/logs/{lid}/lines", params={"start": -5, "end": 5})
    ).status_code == 422


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

    r = await client.get(
        f"/api/v1/logs/{lid}/around-time", params={"timestamp": "not-a-date", "window_sec": 10}
    )
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

    r = await client.get(
        f"/api/v1/logs/{lid}/search", params={"pattern": "[unclosed(", "is_regex": "true"}
    )
    assert r.status_code in (400, 422)


async def test_t2_22_log_search_max_matches_truncation(
    client: Any,
    fake_toolchain: Any,
    mock_llm_server: Any,
    helpers: Any,
) -> None:
    proj = await helpers.create_project(client, key="t2-trunc")
    content = "".join(
        f"2026-11-22 10:00:00.000 ERROR match_target_{i}\n" for i in range(50)
    ).encode("utf-8")
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("m.log", content)],
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
    content = (
        b"2026-11-22 10:00:00.000 INFO arr0.ptr false positive\n"
        b"2026-11-22 10:00:01.000 ERROR arr[0].ptr null\n"
    )
    mock_llm_server.queue_verdict()
    up = await helpers.upload_crash(
        client,
        proj["key"],
        proj["api_key"],
        fake_toolchain.make_dump(),
        log_files=[("lit.log", content)],
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
