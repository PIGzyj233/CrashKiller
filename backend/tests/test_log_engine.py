"""Unit and 100MB stress tests for the 256KB chunked zstd LogEngine."""

from __future__ import annotations

import json
import re
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.log_engine import LogEngine, _parse_ts


def test_mixed_utf8_gbk_bom_crlf_ingestion(tmp_path: Path) -> None:
    engine = LogEngine()
    raw_log = tmp_path / "mixed.log"
    zst_path = tmp_path / "mixed.log.zst"
    idx_path = tmp_path / "mixed.idx.json"

    lines_bytes = [
        (
            b"\xef\xbb\xbf2026-11-22 10:00:00.100 [INFO] "
            b"UTF8\xe5\x90\xaf\xe5\x8a\xa8\xe6\x88\x90\xe5\x8a\x9f\r\n"
        ),
        b"2026-11-22 10:00:01.200 [WARN] "
        + "纹理加载超时 fallback".encode("gb18030")
        + b"\r\n",
        b"2026-11-22 10:00:02.300 [ERROR] "
        + "崩溃前警告：内存池不足".encode("gbk")
        + b"\r\n",
        b"2026-11-22 10:00:03.400 [FATAL] Invalid bytes \xff\xfe recovered\r\n",
    ]
    raw_log.write_bytes(b"".join(lines_bytes))

    result = engine.ingest_log(raw_log, zst_path, idx_path, chunk_size_bytes=256)
    assert result["total_lines"] == 4
    assert result["digest_json"]["error_count"] == 2
    assert result["digest_json"]["warn_count"] == 1

    read_back = engine.read_lines(zst_path, idx_path, 1, 4)
    assert len(read_back) == 4
    assert read_back[0]["text"].startswith("2026-11-22 10:00:00.100")
    assert "UTF8启动成功" in read_back[0]["text"]
    assert "纹理加载超时 fallback" in read_back[1]["text"]
    assert "崩溃前警告：内存池不足" in read_back[2]["text"]
    assert "\ufffd" in read_back[3]["text"]
    assert not read_back[0]["text"].endswith("\r")


def test_chunked_read_lines_and_around_time(tmp_path: Path) -> None:
    engine = LogEngine()
    raw_log = tmp_path / "chunked.log"
    zst_path = tmp_path / "chunked.log.zst"
    idx_path = tmp_path / "chunked.idx.json"

    # Generate 240 lines (~18KB) with 4KB chunks so lines span multiple chunks,
    # including continuation stack lines without timestamps.
    lines: list[str] = []
    for i in range(1, 241):
        sec = i // 4
        minute = sec // 60
        rem_sec = sec % 60
        if i % 40 == 0:
            lines.append(f"2026-11-22 10:{minute:02d}:{rem_sec:02d}.000 [ERROR] Failure #{i}")
            lines.append("    at Renderer::SubmitDrawCall() + 0x1a")
        else:
            lines.append(
                f"2026-11-22 10:{minute:02d}:{rem_sec:02d}.{(i * 17) % 1000:03d} "
                f"[INFO] Tick frame {i} payload={'x' * 30}"
            )
    raw_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = engine.ingest_log(raw_log, zst_path, idx_path, chunk_size_bytes=4096)
    assert len(result["idx_data"]["chunks"]) >= 3
    assert len(result["digest_json"]["tail_lines"]) == 100
    assert len(result["digest_json"]["recent_errors"]) == 6

    # Cross-chunk read_lines
    mid_lines = engine.read_lines(zst_path, idx_path, 45, 95)
    assert len(mid_lines) == 51
    assert mid_lines[0]["line_number"] == 45
    assert mid_lines[-1]["line_number"] == 95

    # Timestamp window read
    window = engine.read_around_time(
        zst_path,
        idx_path,
        target_time=datetime(2026, 11, 22, 10, 0, 30, tzinfo=UTC),
        window_seconds=3,
        max_lines=50,
    )
    assert len(window) > 0
    assert any("Failure #120" in item["text"] for item in window)
    assert any("SubmitDrawCall" in item["text"] for item in window)


def test_100mb_stress_compression_search_latency_and_memory(tmp_path: Path) -> None:
    """Verify >=8:1 compression, O(log N) reads, <500ms regex search, and <20MB RAM on 100MB log."""
    engine = LogEngine()
    raw_log = tmp_path / "stress_100mb.log"
    zst_path = tmp_path / "stress_100mb.log.zst"
    idx_path = tmp_path / "stress_100mb.idx.json"

    target_bytes = 100 * 1024 * 1024  # 100 MiB
    templates = [
        b"2026-11-22 %02d:%02d:%02d.%03d [INFO] RenderThread frame=%d draw_calls=412 gpu_ms=4.21\n",
        (
            b"2026-11-22 %02d:%02d:%02d.%03d [DEBUG] AssetStreamer "
            b"loaded mesh_lod0 id=%d cache_hit=true\n"
        ),
        b"2026-11-22 %02d:%02d:%02d.%03d [INFO] AudioMixer buffer_ok seq=%d latency_ms=11.4\n",
        b"2026-11-22 %02d:%02d:%02d.%03d [WARN] PhysicsSolver substeps=%d clamped dt=0.0167\n",
        b"2026-11-22 %02d:%02d:%02d.%03d [INFO] NetSync packet_ack seq=%d rtt=24ms\n",
    ]
    gbk_tail = " [INFO] 中文客户端资源预加载完成 batch=".encode("gb18030")

    written = 0
    line_num = 0
    marker_lines: list[int] = []
    crash_ts_str = ""

    with open(raw_log, "wb") as fh:
        batch_lines: list[bytes] = []
        while written < target_bytes:
            batch_lines.clear()
            for j in range(500):
                line_num += 1
                total_ms = line_num * 5
                total_sec, ms = divmod(total_ms, 1000)
                total_min, sec = divmod(total_sec, 60)
                hour_off, minute = divmod(total_min, 60)
                hour = 10 + (hour_off % 13)
                if line_num == 5000:
                    batch_lines.append(
                        f"2026-11-22 {hour:02d}:{minute:02d}:{sec:02d}.{ms:03d} "
                        "[ERROR] TextureCache miss fallback null handle=0x0\n".encode("ascii")
                    )
                    marker_lines.append(line_num)
                elif line_num == 550000:
                    batch_lines.append(
                        f"2026-11-22 {hour:02d}:{minute:02d}:{sec:02d}.{ms:03d} "
                        "[ERROR] TextureCache async fallback null tex_id=99\n".encode("ascii")
                    )
                    marker_lines.append(line_num)
                elif written >= int(target_bytes * 0.95) and len(marker_lines) == 2:
                    crash_ts_str = f"2026-11-22 {hour:02d}:{minute:02d}:{sec:02d}.{ms:03d}"
                    batch_lines.append(
                        f"{crash_ts_str} [FATAL] FATAL crash in SubmitDrawCall\n".encode("ascii")
                    )
                    marker_lines.append(line_num)
                elif j % 10 == 0:
                    ts_b = f"2026-11-22 {hour:02d}:{minute:02d}:{sec:02d}.{ms:03d}".encode("ascii")
                    batch_lines.append(ts_b + gbk_tail + f"{line_num}\n".encode("ascii"))
                else:
                    tmpl = templates[j % len(templates)]
                    batch_lines.append(tmpl % (hour, minute, sec, ms, line_num))
            payload = b"".join(batch_lines)
            fh.write(payload)
            written += len(payload)

    ingest_res = engine.ingest_log(
        raw_log,
        zst_path,
        idx_path,
        chunk_size_bytes=262144,
        zstd_level=6,
    )
    raw_size = ingest_res["raw_size"]
    compressed_size = ingest_res["compressed_size"]
    ratio = raw_size / compressed_size

    assert raw_size >= target_bytes
    assert ratio >= 8.0, f"Compression ratio {ratio:.2f} < 8.0"

    # Cross-chunk read_lines near first marker
    first_marker = marker_lines[0]
    slice_lines = engine.read_lines(zst_path, idx_path, first_marker - 5, first_marker + 5)
    assert len(slice_lines) == 11
    assert any("TextureCache miss fallback null" in x["text"] for x in slice_lines)

    # Timestamp window around crash marker
    around = engine.read_around_time(
        zst_path,
        idx_path,
        target_time=crash_ts_str,
        window_seconds=2,
        max_lines=100,
    )
    assert any("FATAL crash in SubmitDrawCall" in x["text"] for x in around)

    # Regex search latency WITHOUT tracemalloc overhead (< 500ms)
    search_res = engine.search(
        zst_path,
        idx_path,
        pattern=r"TextureCache.*fallback\s+null|FATAL\s+crash",
        is_regex=True,
        max_matches=100,
        case_sensitive=False,
    )
    assert search_res["total_matches"] == 3
    assert search_res["elapsed_ms"] < 500.0, (
        f"Regex search took {search_res['elapsed_ms']}ms (>= 500ms)"
    )

    # Separate measurement WITH tracemalloc (< 20MB peak RAM)
    tracemalloc.start()
    try:
        mem_res = engine.search(
            zst_path,
            idx_path,
            pattern=r"TextureCache.*fallback\s+null|FATAL\s+crash",
            is_regex=True,
            max_matches=100,
            case_sensitive=False,
        )
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert mem_res["total_matches"] == 3
    assert peak_bytes < 20 * 1024 * 1024, f"Peak memory {peak_bytes} bytes >= 20MB"

    # Range-regex latency (< 500ms) for both case-sensitive and case-insensitive modes
    for cs in (False, True):
        range_res = engine.search(
            zst_path,
            idx_path,
            pattern=r"[A-Z]{5}_CRASH_NONE|ImpossibleError[0-9]+",
            is_regex=True,
            max_matches=100,
            case_sensitive=cs,
        )
        assert range_res["total_matches"] == 0
        assert range_res["elapsed_ms"] < 500.0, (
            f"Range regex (case_sensitive={cs}) took {range_res['elapsed_ms']}ms (>= 500ms)"
        )

    # Literal-anchored class regex latency (< 500ms) and memory (< 20MB) for cs=True and cs=False
    for cs in (False, True):
        anchored_res = engine.search(
            zst_path,
            idx_path,
            pattern=r"[A-Z]{5}_[0-9]{4}",
            is_regex=True,
            max_matches=100,
            case_sensitive=cs,
        )
        assert anchored_res["total_matches"] == 0
        assert anchored_res["elapsed_ms"] < 500.0, (
            f"Anchored regex (case_sensitive={cs}) took {anchored_res['elapsed_ms']}ms (>= 500ms)"
        )

        tracemalloc.start()
        try:
            anchored_mem = engine.search(
                zst_path,
                idx_path,
                pattern=r"[A-Z]{5}_[0-9]{4}",
                is_regex=True,
                max_matches=100,
                case_sensitive=cs,
            )
            _, anchored_peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert anchored_mem["total_matches"] == 0
        assert anchored_peak < 20 * 1024 * 1024, (
            f"Anchored regex peak memory (case_sensitive={cs}) {anchored_peak} bytes >= 20MB"
        )


def test_search_truncation_and_invalid_regex(tmp_path: Path) -> None:
    engine = LogEngine()
    raw_log = tmp_path / "trunc.log"
    zst_path = tmp_path / "trunc.log.zst"
    idx_path = tmp_path / "trunc.idx.json"

    raw_log.write_text(
        "\n".join(f"2026-11-22 10:00:00.000 [ERROR] needle #{i}" for i in range(15)) + "\n",
        encoding="utf-8",
    )
    engine.ingest_log(raw_log, zst_path, idx_path, chunk_size_bytes=256)

    res = engine.search(zst_path, idx_path, pattern="needle", max_matches=5)
    assert res["total_matches"] == 5
    assert res["truncated"] is True

    with pytest.raises(ValueError):
        engine.search(zst_path, idx_path, pattern="", is_regex=False)

    with pytest.raises(ValueError):
        engine.search(zst_path, idx_path, pattern="[unclosed", is_regex=True)


def test_unicode_regex_search_no_false_negatives(tmp_path: Path) -> None:
    engine = LogEngine()
    raw_log = tmp_path / "unicode.log"
    zst_path = tmp_path / "unicode.log.zst"
    idx_path = tmp_path / "unicode.idx.json"

    raw_log.write_bytes(
        "错误: 纹理加载失败\n".encode() + "警告: 内存不足\n".encode("gb18030")
    )
    engine.ingest_log(raw_log, zst_path, idx_path, chunk_size_bytes=256)

    for cs in (True, False):
        res = engine.search(
            zst_path,
            idx_path,
            pattern=r":\s*\w+",
            is_regex=True,
            case_sensitive=cs,
        )
        assert res["total_matches"] == 2
        assert res["matches"][0]["line_number"] == 1
        assert res["matches"][1]["line_number"] == 2


def test_embedded_cr_and_unicode_line_separators_preserve_line_numbers(tmp_path: Path) -> None:
    engine = LogEngine()
    raw_log = tmp_path / "embedded_cr.log"
    zst_path = tmp_path / "embedded_cr.log.zst"
    idx_path = tmp_path / "embedded_cr.idx.json"

    payload = (
        "2026-11-22 10:00:00 [INFO] a\rb\n"
        "2026-11-22 10:00:01 [INFO] c\u2028d\n"
        "2026-11-22 10:00:02 [ERROR] target\n"
    ).encode("utf-8")
    raw_log.write_bytes(payload)
    res = engine.ingest_log(raw_log, zst_path, idx_path, chunk_size_bytes=4096)
    assert res["total_lines"] == 3

    lines = engine.read_lines(zst_path, idx_path, 1, 10)
    assert [item["line_number"] for item in lines] == [1, 2, 3]
    assert lines[0]["text"] == "2026-11-22 10:00:00 [INFO] a\rb"
    assert lines[1]["text"] == "2026-11-22 10:00:01 [INFO] c\u2028d"
    assert lines[2]["text"] == "2026-11-22 10:00:02 [ERROR] target"

    fast_res = engine.search(zst_path, idx_path, pattern="target", is_regex=False)
    slow_res = engine.search(zst_path, idx_path, pattern=r"target\s*", is_regex=True)
    assert fast_res["matches"] == [{"line_number": 3, "text": "2026-11-22 10:00:02 [ERROR] target"}]
    assert slow_res["matches"] == [{"line_number": 3, "text": "2026-11-22 10:00:02 [ERROR] target"}]

    around = engine.read_around_time(
        zst_path,
        idx_path,
        target_time="2026-11-22 10:00:02",
        window_seconds=0,
    )
    assert len(around) == 1
    assert around[0]["line_number"] == 3


def test_zero_width_regex_no_phantom_eof_match(tmp_path: Path) -> None:
    engine = LogEngine()
    raw_log = tmp_path / "zw.log"
    zst_path = tmp_path / "zw.log.zst"
    idx_path = tmp_path / "zw.idx.json"

    raw_log.write_text("line1\nline2\nline3\n", encoding="utf-8")
    engine.ingest_log(raw_log, zst_path, idx_path, chunk_size_bytes=4096)

    empty_res = engine.search(zst_path, idx_path, pattern=r"^$", is_regex=True)
    assert empty_res["total_matches"] == 0
    assert empty_res["matches"] == []

    all_res = engine.search(zst_path, idx_path, pattern=r".*", is_regex=True)
    assert [m["line_number"] for m in all_res["matches"]] == [1, 2, 3]

    # Real middle empty line must still match
    raw_log2 = tmp_path / "zw2.log"
    zst_path2 = tmp_path / "zw2.log.zst"
    idx_path2 = tmp_path / "zw2.idx.json"
    raw_log2.write_text("line1\n\nline3\n", encoding="utf-8")
    engine.ingest_log(raw_log2, zst_path2, idx_path2, chunk_size_bytes=4096)

    empty_res2 = engine.search(zst_path2, idx_path2, pattern=r"^$", is_regex=True)
    assert empty_res2["matches"] == [{"line_number": 2, "text": ""}]


def test_read_around_time_out_of_order_timestamps(tmp_path: Path) -> None:
    engine = LogEngine()
    raw_log = tmp_path / "ooo.log"
    zst_path = tmp_path / "ooo.log.zst"
    idx_path = tmp_path / "ooo.idx.json"

    pad = "x" * 180
    lines = [
        f"2026-11-22 10:00:10.000 [INFO] chunk0_a {pad}",
        f"2026-11-22 10:00:11.000 [INFO] chunk0_b {pad}",
        f"2026-11-22 10:00:30.000 [INFO] chunk1_ooo_high {pad}",
        f"2026-11-22 10:00:31.000 [INFO] chunk1_ooo_high2 {pad}",
        f"2026-11-22 10:00:15.000 [ERROR] chunk2_target_a {pad}",
        f"2026-11-22 10:00:16.000 [ERROR] chunk2_target_b {pad}",
    ]
    raw_log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ingest_res = engine.ingest_log(raw_log, zst_path, idx_path, chunk_size_bytes=350)
    assert len(ingest_res["idx_data"]["chunks"]) >= 3
    assert "min_ts" in ingest_res["idx_data"]["chunks"][0]
    assert "max_ts" in ingest_res["idx_data"]["chunks"][0]

    around = engine.read_around_time(
        zst_path,
        idx_path,
        target_time="2026-11-22 10:00:15.500",
        window_seconds=2,
        max_lines=50,
    )
    texts = [item["text"] for item in around]
    assert any("chunk2_target_a" in t for t in texts)
    assert any("chunk2_target_b" in t for t in texts)


def test_empty_alternation_and_optional_group_regex_soundness(tmp_path: Path) -> None:
    engine = LogEngine()
    raw_log = tmp_path / "alt.log"
    zst_path = tmp_path / "alt.log.zst"
    idx_path = tmp_path / "alt.idx.json"

    raw_log.write_text("alpha\nbeta\ngamma_target\n", encoding="utf-8")
    engine.ingest_log(raw_log, zst_path, idx_path, chunk_size_bytes=256)

    # Empty branch inside group must not falsely require 'missing_tok'
    res1 = engine.search(zst_path, idx_path, pattern=r"(missing_tok|)alpha", is_regex=True)
    assert res1["total_matches"] == 1
    assert res1["matches"][0]["line_number"] == 1

    # Top-level empty branch matches every line
    res2 = engine.search(zst_path, idx_path, pattern=r"nonexistent|", is_regex=True)
    assert res2["total_matches"] == 3


def test_search_differential_oracle_tricky_patterns(tmp_path: Path) -> None:
    """Compare LogEngine.search() against line-by-line Python re oracle on tricky cases."""
    engine = LogEngine()
    raw_log = tmp_path / "oracle.log"
    zst_path = tmp_path / "oracle.log.zst"
    idx_path = tmp_path / "oracle.idx.json"

    lines = [
        "",
        "alpha",
        "Error",
        "ERROR",
        "ErrorLog",
        "errorLog",
        "foo",
        "bar",
        "baz",
        "foobar",
        "foo bar",
        "CRASH",
        "crash",
        "cross_start",
        "cross_end",
        "a:bar",
        "fooa:bar",
        "foobaz",
        "ABC",
        "abc",
        "\xe9",
        "\xc9",
        "[]A-z]",
        "e\u2028f",
        "Kelvin_\u212a_sign",
        "kelvin_k_sign",
        "is_turkish_i",
        "IS_TURKISH_I",
        "\u0130S_TURKISH_I",
        "\u0131s_turkish_i",
        "ABCDE_1234",
        "abcde_1234",
        "12345",
        "a   b",
        "   ",
        "ERROR: texture_missing",
        "FATAL : shader_init",
        "foofoo",
        "错误: 纹理加载失败",
        "",
    ]
    raw_log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Small chunks to ensure cross-chunk boundaries and multiline anchors are exercised
    ingest_res = engine.ingest_log(raw_log, zst_path, idx_path, chunk_size_bytes=120)
    assert len(ingest_res["idx_data"]["chunks"]) >= 3

    oracle_cases: list[tuple[str, bool, bool]] = [
        (r"[\w]+", True, True),
        (r"[\w]+", True, False),
        (r"^[\w]+$", True, True),
        (r"a[\s]+b", True, True),
        (r"^[\s]+$", True, True),
        (r"^[\d]+$", True, True),
        (r"(?mi:Error)", True, True),
        (r"(?mx)foo bar", True, True),
        (r"(?si:CRASH)", True, True),
        (r"(?ai)CRASH", True, True),
        (r"(?is)cross_start.*cross_end", True, True),
        (r"(foo)?(?(1)a:bar)", True, True),
        (r"(foo)?(?(1)a:bar|baz)", True, True),
        (r"(?>foo|a:bar)", True, True),
        (r"\x41BC", True, True),
        (r"\u0041BC", True, True),
        (r"\xe9", True, True),
        (r"\xe9", True, False),
        (r"\351", True, True),
        (r"\N{LATIN CAPITAL LETTER A}BC", True, True),
        (r"[]A-z]", True, True),
        (r"[]A-z]", True, False),
        (r"e\u2028f", True, True),
        (r"Kelvin_\u212a_sign", True, False),
        (r"kelvin_k_sign", True, False),
        (r"Kelvin_\u212a_sign", False, False),
        (r"kelvin_k_sign", False, False),
        (r"is_turkish_i", True, False),
        (r"is_turkish_i", False, False),
        (r"(?-i:Error)Log", True, False),
        (r"(?-m:^foo)", True, True),
        (r"\Afoo", True, True),
        (r"\B", True, True),
        (r"^\B$", True, True),
        (r"^$", True, True),
        (r"(?P<tag>ERROR|FATAL)\s*:\s*\w+", True, True),
        (r"(?P<w>foo)(?P=w)", True, True),
        (r"(?<=ABCDE_)[0-9]{4}", True, True),
        (r"[A-Z]{5}_[0-9]{4}", True, True),
        (r"[A-Z]{5}_[0-9]{4}", True, False),
    ]

    for pat, is_rx, cs in oracle_cases:
        flags = re.MULTILINE | (0 if cs else re.IGNORECASE)
        rx = re.compile(pat if is_rx else re.escape(pat), flags)
        expected = [
            {"line_number": i + 1, "text": ln}
            for i, ln in enumerate(lines)
            if rx.search(ln)
        ]
        res = engine.search(
            zst_path,
            idx_path,
            pattern=pat,
            is_regex=is_rx,
            max_matches=1000,
            case_sensitive=cs,
        )
        assert res["matches"] == expected, (
            f"Oracle mismatch for pat={pat!r}, is_regex={is_rx}, case_sensitive={cs}"
        )


def test_read_around_time_untimed_continuations_legacy_idx_and_stale_ts(tmp_path: Path) -> None:
    """Verify untimed continuation chunks, legacy idx without min/max, and stale current_ts."""
    engine = LogEngine()
    raw_log = tmp_path / "untimed.log"
    zst_path = tmp_path / "untimed.log.zst"
    idx_path = tmp_path / "untimed.idx.json"

    pad = "x" * 180
    lines = [
        f"2026-11-22 10:00:15.000 [ERROR] chunk0_head {pad}",
        f"    untimed_frame_1 {pad}",
        f"    untimed_frame_2 {pad}",
        f"    untimed_frame_3 {pad}",
        f"2026-11-22 10:00:30.000 [INFO] chunk_after_window {pad}",
    ]
    raw_log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ingest_res = engine.ingest_log(raw_log, zst_path, idx_path, chunk_size_bytes=200)
    assert len(ingest_res["idx_data"]["chunks"]) >= 4

    around = engine.read_around_time(
        zst_path,
        idx_path,
        target_time="2026-11-22 10:00:15",
        window_seconds=2,
        max_lines=50,
    )
    assert [m["line_number"] for m in around] == [1, 2, 3, 4]

    # Query strictly after the max timestamp in the entire file -> []
    assert (
        engine.read_around_time(
            zst_path,
            idx_path,
            target_time="2026-11-22 10:05:00",
            window_seconds=2,
            max_lines=50,
        )
        == []
    )

    # Legacy .idx.json without min_ts / max_ts fields
    legacy_idx_path = tmp_path / "legacy.idx.json"
    idx_data = json.loads(idx_path.read_text(encoding="utf-8"))
    for ch in idx_data["chunks"]:
        ch.pop("min_ts", None)
        ch.pop("max_ts", None)
    legacy_idx_path.write_text(json.dumps(idx_data), encoding="utf-8")

    around_legacy = engine.read_around_time(
        zst_path,
        legacy_idx_path,
        target_time="2026-11-22 10:00:15",
        window_seconds=2,
        max_lines=50,
    )
    assert [m["line_number"] for m in around_legacy] == [1, 2, 3, 4]

    # Stale current_ts across skipped out-of-window chunk must not leak into untimed continuation
    raw2 = tmp_path / "stale.log"
    zst2 = tmp_path / "stale.log.zst"
    idx2 = tmp_path / "stale.idx.json"
    lines2 = [
        f"2026-11-22 10:00:15.000 [INFO] c0_in_window {pad}",
        f"2026-11-22 10:00:30.000 [INFO] c1_out_of_window {pad}",
        f"    c2_untimed_belongs_to_10_00_30 {pad}",
        f"2026-11-22 10:00:16.000 [INFO] c3_in_window {pad}",
    ]
    raw2.write_text("\n".join(lines2) + "\n", encoding="utf-8")
    engine.ingest_log(raw2, zst2, idx2, chunk_size_bytes=200)

    around2 = engine.read_around_time(
        zst2,
        idx2,
        target_time="2026-11-22 10:00:15.500",
        window_seconds=2,
        max_lines=50,
    )
    assert [m["line_number"] for m in around2] == [1, 4]


def test_parse_ts_invalid_tz_offset_no_crash(tmp_path: Path) -> None:
    """Out-of-range timezone offsets like +25:00 must return None instead of crashing."""
    assert _parse_ts("2026-11-22 10:00:00+25:00") is None
    assert _parse_ts("2026-11-22T10:00:00-25:00") is None

    engine = LogEngine()
    raw_log = tmp_path / "bad_tz.log"
    zst_path = tmp_path / "bad_tz.log.zst"
    idx_path = tmp_path / "bad_tz.idx.json"
    raw_log.write_text("2026-11-22 10:00:00+25:00 [INFO] bad tz\n", encoding="utf-8")

    res = engine.ingest_log(raw_log, zst_path, idx_path, chunk_size_bytes=256)
    assert res["total_lines"] == 1
    rows = engine.read_lines(zst_path, idx_path, 1, 1)
    assert len(rows) == 1
    assert rows[0]["timestamp"] is None
    assert rows[0]["level"] == "INFO"

    # Nanosecond + timezone offset preserves UTC conversion
    nano_dt = _parse_ts("2026-11-22T10:00:06.123456789+08:00")
    assert nano_dt is not None
    assert nano_dt.hour == 2
    assert nano_dt.microsecond == 123456


def test_session_midfile_restart_digest_boundary_and_filtering(tmp_path: Path) -> None:
    """Mid-file crashing session followed by untagged fatal + process restart + new session."""
    from app.services.log_engine import normalize_session_id

    assert normalize_session_id("  flow-mid-42  ") == "flow-mid-42"
    assert normalize_session_id("   ") is None
    with pytest.raises(ValueError):
        normalize_session_id("bad\nsession")
    with pytest.raises(ValueError):
        normalize_session_id("x" * 129)

    lines: list[str] = []
    # Lines 1..20: earlier session
    for i in range(1, 21):
        lvl = "ERROR" if i == 10 else "INFO"
        lines.append(f"2026-11-24 09:00:{i:02d}.000 {lvl} [old-sess-000] earlier activity {i}")
    # Lines 21..35: target crashing session (mixed case in log vs lowercase upload)
    for i in range(1, 16):
        if i == 5:
            lvl = "WARN"
        elif i in (12, 15):
            lvl = "ERROR"
        else:
            lvl = "INFO"
        lines.append(
            f"2026-11-24 10:15:{i:02d}.000 {lvl} [Flow-MID-42] render step {i} payload={'z' * 24}"
        )
    # Lines 36..40: untagged crash moment & process restart boundary
    lines.append("2026-11-24 10:15:15.500 FATAL Unhandled exception 0xC0000005 writing dump")
    lines.append("2026-11-24 10:15:18.000 INFO === Process auto-restarted pid=9912 ===")
    # Lines 38..97: 60 lines of post-restart new session with unrelated errors
    for i in range(1, 61):
        lvl = "ERROR" if i % 10 == 0 else "INFO"
        lines.append(
            f"2026-11-24 11:00:{i % 60:02d}.000 {lvl} "
            f"[new-sess-999] post-restart work {i} {'q' * 24}"
        )

    raw_log = tmp_path / "restart_multi.log"
    zst_path = tmp_path / "restart_multi.log.zst"
    idx_path = tmp_path / "restart_multi.idx.json"
    raw_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    engine = LogEngine()
    res = engine.ingest_log(
        raw_log,
        zst_path,
        idx_path,
        chunk_size_bytes=512,
        session_id="flow-mid-42",
    )

    digest = res["digest_json"]
    sess = digest["session"]
    assert sess["session_id"] == "flow-mid-42"
    assert sess["match_count"] == 15
    assert sess["error_count"] == 2
    assert sess["warn_count"] == 1
    assert sess["first_line"] == 21
    assert sess["last_line"] == 35
    assert sess["first_ts"].startswith("2026-11-24T10:15:01")
    assert sess["last_ts"].startswith("2026-11-24T10:15:15")
    assert sess["trailing_lines_after_session"] == len(lines) - 35
    assert [e["line_number"] for e in sess["recent_errors"]] == [32, 35]
    assert len(sess["tail_lines"]) == 15
    # Post-session lines capture untagged FATAL + restart banner right after L35
    assert len(sess["post_session_lines"]) == 30
    assert sess["post_session_lines"][0]["line_number"] == 36
    assert "Unhandled exception" in sess["post_session_lines"][0]["text"]
    assert "auto-restarted" in sess["post_session_lines"][1]["text"]

    # Indexed fast-path pagination over session lines preserves real line numbers
    page1 = engine.read_session_lines(zst_path, idx_path, "flow-mid-42", 1, 5)
    assert page1["total_matches"] == 15
    assert page1["start_index"] == 1
    assert page1["end_index"] == 5
    assert [r["line_number"] for r in page1["lines"]] == [21, 22, 23, 24, 25]

    page2 = engine.read_session_lines(zst_path, idx_path, "FLOW-MID-42", 11, 20)
    assert page2["total_matches"] == 15
    assert page2["start_index"] == 11
    assert page2["end_index"] == 15
    assert [r["line_number"] for r in page2["lines"]] == [31, 32, 33, 34, 35]

    # Session-filtered search skips unrelated chunks and ignores post-restart errors
    search_res = engine.search(
        zst_path,
        idx_path,
        pattern="ERROR",
        session_id="flow-mid-42",
    )
    assert [m["line_number"] for m in search_res["matches"]] == [32, 35]
    assert search_res["scanned_chunks"] < len(res["idx_data"]["chunks"])

    # Session-filtered read_around_time returns only target session lines
    around_res = engine.read_around_time(
        zst_path,
        idx_path,
        target_time="2026-11-24 10:15:15.000",
        window_seconds=3,
        session_id="flow-mid-42",
    )
    assert [r["line_number"] for r in around_res] == [32, 33, 34, 35]

    # Legacy index fallback (without session_id / session_match_count) still works identically
    legacy_idx = json.loads(idx_path.read_text(encoding="utf-8"))
    legacy_idx.pop("session_id", None)
    for chunk in legacy_idx["chunks"]:
        chunk.pop("session_match_count", None)
    legacy_idx_path = tmp_path / "legacy.idx.json"
    legacy_idx_path.write_text(json.dumps(legacy_idx), encoding="utf-8")

    fallback_page = engine.read_session_lines(zst_path, legacy_idx_path, "flow-mid-42", 1, 5)
    assert fallback_page["total_matches"] == 15
    assert [r["line_number"] for r in fallback_page["lines"]] == [21, 22, 23, 24, 25]

