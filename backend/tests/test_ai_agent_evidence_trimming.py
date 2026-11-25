"""Unit tests for tool output trimming, structured evidence, and session log tools."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    CrashLog,
    CrashReport,
    Issue,
    Project,
)
from app.services.ai_agent import (
    AITriageAgent,
    _prepare_tool_output_for_llm,
)
from app.services.log_engine import LogEngine
from app.services.storage import StorageManager
from tests._ai_agent_helpers import (
    FakeOpenAIClient,
    _investigate_then_verdict,
    _make_response,
    _seed_crash_with_log,
)


def test_trim_never_shortens_instruction_address() -> None:
    """P1 regression: an address that does not fit whole is omitted, never clipped."""
    ip = "0x00007ff700001234"
    frame = {"frame_index": 0, "ip": ip, "module": None, "function": None}
    payload: dict[str, Any] = {"top_frames": [frame], "missing_symbols": []}
    skeleton_len = len(json.dumps({**payload, "top_frames": []}))
    # Room for a few short fields, but not for the 20-char address.
    max_chars = skeleton_len + len("\n...[TRUNCATED]") + 45

    _, trimmed = _prepare_tool_output_for_llm(
        "get_crash_overview", json.dumps(payload), max_chars
    )

    assert trimmed is not None
    frames = trimmed["top_frames"]
    assert all(f.get("ip") in (None, ip) for f in frames)
    # Without a whole identifier the frame is no longer crash evidence.
    assert frames == []


def test_trim_does_not_treat_a_function_preview_as_full_stack_evidence() -> None:
    function = "R::flushPending<" + "X" * 3000 + ">"
    payload = {"top_frames": [{"frame_index": 0, "function": function}], "missing_symbols": []}
    preview_payload = {**payload, "top_frames": [{"frame_index": 0, "function_preview": "R::f"}]}
    max_chars = len(json.dumps(preview_payload)) + len("\n...[TRUNCATED]")

    delivered, trimmed = _prepare_tool_output_for_llm(
        "get_crash_overview", json.dumps(payload), max_chars
    )

    assert trimmed is not None
    assert trimmed["top_frames"] == []
    assert json.loads(delivered.removesuffix("\n...[TRUNCATED]")) == trimmed


def test_trim_keeps_log_body_before_timestamp_and_level() -> None:
    """P2 regression: visible log text takes budget before timestamp/level metadata."""
    entry = {
        "line_number": 7,
        "text": "FATAL: " + "x" * 3000,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "level": "FATAL",
    }
    payload: dict[str, Any] = {"log_id": "abc", "lines": [entry]}
    skeleton_len = len(json.dumps({**payload, "lines": []}))
    # Room for line_number and ~10 chars of text, but not timestamp plus level as well.
    max_chars = skeleton_len + len("\n...[TRUNCATED]") + 40

    _, trimmed = _prepare_tool_output_for_llm(
        "read_log_lines", json.dumps(payload), max_chars
    )

    assert trimmed is not None
    assert len(trimmed["lines"]) == 1
    kept = trimmed["lines"][0]
    assert kept["line_number"] == 7
    assert kept["text"].startswith("FATAL:")


def test_trim_keeps_next_log_entry_after_short_entry() -> None:
    """P2 regression: a short INFO then a long FATAL must keep the FATAL (clipped).

    Before the fix, _fit_partial_entry was only called when fitted == 0.  With a short
    INFO already placed, fitted == 1 and the long FATAL was silently discarded.
    """
    short_entry = {"line_number": 1, "text": "INFO start", "timestamp": None, "level": "INFO"}
    long_text = "FATAL: " + "x" * 3000
    long_entry = {"line_number": 2, "text": long_text, "timestamp": None, "level": "FATAL"}
    payload = {
        "log_id": "abc",
        "total_lines": 2,
        "start_line": 1,
        "end_line": 2,
        "capped_to_200": False,
        "lines": [short_entry, long_entry],
    }
    raw = json.dumps(payload)
    # Budget: fits short_entry but not long_entry in full; leaves ~200 chars spare for a clip.
    short_only_len = len(json.dumps({**payload, "lines": [short_entry]}))
    max_chars = short_only_len + 200

    delivered, trimmed = _prepare_tool_output_for_llm("read_log_lines", raw, max_chars)

    assert trimmed is not None
    lines = trimmed["lines"]
    assert len(lines) == 2, "both entries must be present in trimmed output"
    assert lines[0]["text"] == "INFO start"
    assert lines[1]["text"].startswith("FATAL:"), "FATAL text must be partially retained"
    assert len(lines[1]["text"]) < len(long_text), "long text must have been clipped"
    assert delivered.endswith("...[TRUNCATED]")


def test_trim_keeps_frame_short_fields_when_function_too_long() -> None:
    """P2 regression: a frame whose function name is too long must still deliver ip and module.

    Before the fix, top_frames had no partial-entry path, so a frame that did not fit as
    a whole was dropped entirely, losing the instruction address and module name.
    """
    long_func = "LongNamespace::" + "A" * 4000 + "::method(int, int)"
    frame = {
        "frame_index": 0,
        "ip": "0x00007ff700001234",
        "module": "engine.dll",
        "function": long_func,
        "file": None,
    }
    payload: dict[str, Any] = {
        "top_frames": [frame],
        "missing_symbols": [],
        "logs": [],
        "candidate_issues": [],
    }
    raw = json.dumps(payload)
    # Budget: fits the empty-list skeleton plus ~200 chars — enough for ip/module but not
    # the multi-kilobyte function name.
    skeleton_len = len(json.dumps({**payload, "top_frames": []}))
    max_chars = skeleton_len + 200

    delivered, trimmed = _prepare_tool_output_for_llm("get_crash_overview", raw, max_chars)

    assert trimmed is not None
    frames = trimmed["top_frames"]
    assert len(frames) == 1, "partial frame must be retained"
    assert frames[0].get("ip") == "0x00007ff700001234", "instruction address must be preserved"
    assert frames[0].get("module") == "engine.dll", "module name must be preserved"
    assert "function" not in frames[0]
    preview = frames[0]["function_preview"]
    assert 0 < len(preview) < len(long_func)
    assert long_func.startswith(preview)
    assert len(delivered) <= max_chars
    assert delivered.endswith("...[TRUNCATED]")


async def test_structured_evidence_blank_logs_second_thread_and_ip_only_frames(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """Verify exact structured evidence validation for all three edge cases:
    1) Default 8000-char limit with 130 blank log lines followed by '5' (truncated out):
       line numbers must never spoof log text -> FAILED and no Issue created.
    2) get_other_threads where first non-crashing thread has frames=[] and second thread
       has visible frames -> counts as valid crash evidence and completes.
    3) Unsymbolicated top_frames containing only instruction address `ip` -> counts as
       valid crash evidence and supports a COMPLETED NEEDS_REVIEW report.
    """
    proj, crash, _ = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path, with_log=False
    )

    # 1. Log with 150 blank lines followed by a single non-empty line "5" that gets trimmed out
    # under the default 8000-char tool output limit.
    blank_log_id = uuid.uuid4()
    raw_blank_log = tmp_path / f"blank_{blank_log_id.hex}.log"
    raw_blank_log.write_bytes((b"\n" * 150) + b"5\n")
    rel_zst = f"logs/{crash.id}/{blank_log_id}.log.zst"
    rel_idx = f"logs/{crash.id}/{blank_log_id}.idx.json"
    abs_zst = storage_manager.resolve_path(rel_zst)
    abs_idx = storage_manager.resolve_path(rel_idx)
    abs_zst.parent.mkdir(parents=True, exist_ok=True)
    ingest_info = LogEngine().ingest_log(raw_blank_log, abs_zst, abs_idx)
    blank_log = CrashLog(
        id=blank_log_id,
        crash_id=crash.id,
        filename="blank.log",
        zst_path=rel_zst,
        idx_path=rel_idx,
        raw_size=ingest_info["raw_size"],
        compressed_size=ingest_info["compressed_size"],
        total_lines=ingest_info["total_lines"],
        digest_json=ingest_info["digest_json"],
    )
    db_session.add(blank_log)
    await db_session.commit()

    agent = AITriageAgent(storage=storage_manager, settings=test_settings)

    def blank_lines_handler(step: int, _k: dict[str, Any]) -> Any:
        if step == 1:
            return _make_response(
                tool_calls=[
                    {
                        "id": "bl1",
                        "name": "read_log_lines",
                        "arguments": {
                            "log_id": str(blank_log.id),
                            "start_line": 1,
                            "end_line": 151,
                        },
                    }
                ]
            )
        return _make_response(
            content=json.dumps(
                {
                    "verdict": "NEW_ISSUE",
                    "confidence": 0.9,
                    "suggested_title": "Hallucinated From Blank Log Lines",
                    "root_cause_analysis": "Guessed despite only receiving blank log lines.",
                    "reasoning_summary": "Line 151 was truncated out.",
                }
            )
        )

    blank_client = FakeOpenAIClient(blank_lines_handler)
    run_blank = await agent.run_triage(
        db_session,
        crash,
        settings_override={"ai_max_steps": 20, "ai_tool_output_max_chars": 8000},
        openai_client=blank_client,
    )
    assert run_blank.status == "FAILED"
    tool_msg_content = next(
        m["content"] for m in blank_client.calls[1]["messages"] if m["role"] == "tool"
    )
    assert tool_msg_content.endswith("...[TRUNCATED]")
    delivered_json = json.loads(tool_msg_content[: -len("\n...[TRUNCATED]")])
    assert len(delivered_json["lines"]) > 0
    assert all(item["text"] == "" for item in delivered_json["lines"])
    await db_session.refresh(crash)
    assert crash.issue_id is None
    assert (
        await db_session.execute(select(Issue).where(Issue.project_id == proj.id))
    ).scalars().all() == []

    # 2. First non-crashing thread has empty frames=[], second has real frames -> valid evidence.
    crash.parsed_stacks_json = {
        "crashing_thread_index": 0,
        "threads": [
            {"thread_index": 0, "thread_id": 100, "thread_name": "Main", "frames": []},
            {"thread_index": 1, "thread_id": 101, "thread_name": "EmptyWorker", "frames": []},
            {
                "thread_index": 2,
                "thread_id": 102,
                "thread_name": "RealWorker",
                "frames": [
                    {
                        "frame_index": 0,
                        "module": "IO.dll",
                        "function": "DiskCache::Flush()",
                        "ip": "0x00007ff700001234",
                    }
                ],
            },
        ],
    }
    await db_session.commit()

    def other_threads_handler(step: int, _k: dict[str, Any]) -> Any:
        if step == 1:
            return _make_response(
                tool_calls=[{"id": "ot1", "name": "get_other_threads", "arguments": {}}]
            )
        return _make_response(
            content=json.dumps(
                {
                    "verdict": "NEW_ISSUE",
                    "confidence": 0.82,
                    "suggested_title": "DiskCache Flush Hang",
                    "root_cause_analysis": "RealWorker thread stuck in IO.dll!DiskCache::Flush().",
                    "reasoning_summary": "Inspected non-crashing threads via get_other_threads.",
                }
            )
        )

    run_threads = await agent.run_triage(
        db_session, crash, openai_client=FakeOpenAIClient(other_threads_handler)
    )
    assert run_threads.status == "COMPLETED"
    await db_session.refresh(crash)
    assert crash.ai_verdict == "NEW_ISSUE"
    assert crash.issue_id is not None

    # 3. Unsymbolicated crashing frame with only instruction address `ip` supports NEEDS_REVIEW.
    ip_only_crash = CrashReport(
        project_id=proj.id,
        status="TRIAGING",
        app_version="1.0.0",
        crash_reason="EXCEPTION_ACCESS_VIOLATION_READ",
        parsed_stacks_json={
            "crashing_thread_index": 0,
            "threads": [
                {
                    "thread_index": 0,
                    "thread_id": 300,
                    "thread_name": "Main",
                    "frames": [
                        {
                            "frame_index": 0,
                            "ip": "0x00007ff712345678",
                            "module": None,
                            "function": None,
                            "missing_symbols": True,
                        }
                    ],
                }
            ],
        },
        missing_symbols_json=[],
    )
    db_session.add(ip_only_crash)
    await db_session.commit()

    ip_only_client = FakeOpenAIClient(
        _investigate_then_verdict(
            json.dumps(
                {
                    "verdict": "NEEDS_REVIEW",
                    "confidence": 0.25,
                    "suggested_title": "Unsymbolicated crash at 0x00007ff712345678",
                    "root_cause_analysis": (
                        "Top frame only exposes raw instruction address 0x00007ff712345678 "
                        "without loaded module or PDB symbols."
                    ),
                    "reasoning_summary": (
                        "Called get_crash_overview and confirmed only raw IP is available."
                    ),
                }
            )
        )
    )
    run_ip_only = await agent.run_triage(db_session, ip_only_crash, openai_client=ip_only_client)
    assert run_ip_only.status == "COMPLETED"
    await db_session.refresh(ip_only_crash)
    assert ip_only_crash.ai_verdict == "NEEDS_REVIEW"
    assert ip_only_crash.ai_confidence == 0.25


async def test_ai_agent_session_midfile_restart_tools_fallback_and_trim_priority(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """AI agent gets mid-file session context, related crashes, fallback, and trim priority."""
    proj = Project(
        key=f"sess-ai-{uuid.uuid4().hex[:6]}",
        name="Session AI Project",
        api_key_hash=hashlib.sha256(b"key").hexdigest(),
        settings_json={"session_id_label": "FlowID"},
    )
    db_session.add(proj)
    await db_session.flush()

    # Delayed manual upload: crash_time is 4 hours AFTER the actual crashing session in the log
    delayed_upload_time = datetime(2026, 11, 24, 14, 0, 0, tzinfo=UTC)
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
                        "function": "Renderer::Present()",
                        "module_offset": "0x200",
                        "file": "present.cpp",
                        "line": 88,
                        "missing_symbols": False,
                        "inlines": [],
                    }
                ],
            }
        ],
        "modules": [],
    }
    crash = CrashReport(
        project_id=proj.id,
        status="TRIAGING",
        app_version="2.1.0",
        session_id="flow-crash-777",
        crash_reason="EXCEPTION_ACCESS_VIOLATION_READ",
        crash_address="0x0",
        parsed_summary_json={"top_frames": stacks["threads"][0]["frames"]},
        parsed_stacks_json=stacks,
        missing_symbols_json=[],
        crash_time=delayed_upload_time,
    )
    sibling = CrashReport(
        project_id=proj.id,
        status="ANALYZED",
        app_version="2.1.0",
        session_id="flow-crash-777",
        crash_reason="EXCEPTION_ILLEGAL_INSTRUCTION",
        crash_address="0x40",
        crash_time=delayed_upload_time - timedelta(minutes=1),
    )
    db_session.add_all([crash, sibling])
    await db_session.flush()

    log_id = uuid.uuid4()
    raw_log = tmp_path / "midfile_ai.log"
    raw_log.write_text(
        "\n".join(
            [
                "2026-11-24 09:00:01.000 INFO [old-flow-001] old boot",
                "2026-11-24 10:00:01.000 INFO [flow-crash-777] streaming start",
                "2026-11-24 10:00:08.000 ERROR [flow-crash-777] DXGI_ERROR_DEVICE_REMOVED",
                "2026-11-24 10:00:08.200 FATAL Unhandled exception 0xC0000005 writing minidump",
                "2026-11-24 10:00:12.000 INFO === Process auto-restarted ===",
                "2026-11-24 13:55:00.000 ERROR [restarted-flow-888] unrelated network timeout",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    rel_zst = f"logs/{crash.id}/{log_id}.log.zst"
    rel_idx = f"logs/{crash.id}/{log_id}.idx.json"
    abs_zst = storage_manager.resolve_path(rel_zst)
    abs_idx = storage_manager.resolve_path(rel_idx)
    abs_zst.parent.mkdir(parents=True, exist_ok=True)
    ingest_res = LogEngine().ingest_log(
        raw_log,
        abs_zst,
        abs_idx,
        session_id=crash.session_id,
    )
    db_session.add(
        CrashLog(
            id=log_id,
            crash_id=crash.id,
            filename="runtime.log",
            zst_path=rel_zst,
            idx_path=rel_idx,
            raw_size=ingest_res["raw_size"],
            compressed_size=ingest_res["compressed_size"],
            total_lines=ingest_res["total_lines"],
            digest_json=ingest_res["digest_json"],
        )
    )
    await db_session.commit()

    observed_tools: dict[str, dict[str, Any]] = {}

    def handler(step: int, kwargs: dict[str, Any]) -> Any:
        messages = kwargs["messages"]
        if step == 1:
            assert "session_id [FlowID]=flow-crash-777" in messages[1]["content"]
            return _make_response(
                tool_calls=[{"id": "c1", "name": "get_crash_overview", "arguments": {}}]
            )
        if step == 2:
            observed_tools["overview"] = json.loads(messages[-1]["content"])
            return _make_response(
                tool_calls=[
                    {
                        "id": "c2",
                        "name": "get_log_summary",
                        "arguments": {"log_id": str(log_id)},
                    }
                ]
            )
        if step == 3:
            observed_tools["summary"] = json.loads(messages[-1]["content"])
            return _make_response(
                tool_calls=[
                    {
                        "id": "c3",
                        "name": "read_log_around_time",
                        "arguments": {"log_id": str(log_id), "only_session": True},
                    }
                ]
            )
        if step == 4:
            observed_tools["around"] = json.loads(messages[-1]["content"])
            return _make_response(
                tool_calls=[
                    {
                        "id": "c4",
                        "name": "search_log",
                        "arguments": {
                            "log_id": str(log_id),
                            "pattern": "ERROR",
                            "only_session": True,
                        },
                    }
                ]
            )
        if step == 5:
            observed_tools["search"] = json.loads(messages[-1]["content"])
            return _make_response(
                content=json.dumps(
                    {
                        "verdict": "NEW_ISSUE",
                        "matched_issue_id": None,
                        "confidence": 0.93,
                        "suggested_title": "DXGI device removed in Renderer::Present",
                        "root_cause_analysis": (
                            "Mid-file session flow-crash-777 hit DXGI_ERROR_DEVICE_REMOVED at L3 "
                            "followed by untagged fatal crash at L4 before process restart."
                        ),
                        "reasoning_summary": (
                            "Isolated flow-crash-777 from post-restart noise via session tools."
                        ),
                        "evidence_log_snippets": [
                            {
                                "log_id": str(log_id),
                                "start_line": 2,
                                "end_line": 4,
                                "relevance": "Session error and untagged crash boundary",
                            }
                        ],
                        "key_log_patterns": ["DXGI_ERROR_DEVICE_REMOVED"],
                        "suggested_component": "Rendering",
                    }
                )
            )
        raise AssertionError(f"Unexpected step {step}")

    agent = AITriageAgent(storage=storage_manager, settings=test_settings)
    run = await agent.run_triage(db_session, crash, openai_client=FakeOpenAIClient(handler))
    assert run.status == "COMPLETED"

    ov = observed_tools["overview"]
    assert ov["session_id"] == "flow-crash-777"
    assert ov["session_id_label"] == "FlowID"
    assert ov["related_session_crash_count"] == 1
    assert ov["related_session_crashes"][0]["crash_id"] == str(sibling.id)
    assert ov["logs"][0]["session_first_line"] == 2
    assert ov["logs"][0]["session_last_line"] == 3
    assert ov["logs"][0]["trailing_lines_after_session"] == 3

    sm = observed_tools["summary"]
    assert sm["session_id"] == "flow-crash-777"
    assert [e["line_number"] for e in sm["session_recent_errors"]] == [3]
    assert len(sm["post_session_lines"]) == 3
    assert "Unhandled exception" in sm["post_session_lines"][0]["text"]

    # Automatic fallback from delayed upload crash_time to session_last_ts
    around = observed_tools["around"]
    assert around["session_id"] == "flow-crash-777"
    assert around["anchored_to"] == "session_last_ts"
    assert [ln["line_number"] for ln in around["lines"]] == [2, 3]

    srch = observed_tools["search"]
    assert srch["session_id"] == "flow-crash-777"
    assert [m["line_number"] for m in srch["matches"]] == [3]

    # Tool output trimmer prioritizes session errors/tail/post-session over global noise
    noisy_summary = {
        "log_id": str(log_id),
        "filename": "runtime.log",
        "session_id": "flow-crash-777",
        "session_recent_errors": [
            {"line_number": 3, "text": "DXGI_ERROR_DEVICE_REMOVED [flow-crash-777]"}
        ],
        "session_tail_lines": [
            {"line_number": 2, "text": "streaming start [flow-crash-777]"}
        ],
        "post_session_lines": [
            {"line_number": 4, "text": "FATAL Unhandled exception writing minidump"}
        ],
        "recent_errors": [
            {"line_number": 100 + i, "text": f"unrelated post-restart error {i} " + ("N" * 80)}
            for i in range(20)
        ],
        "tail_lines": [
            {"line_number": 200 + i, "text": f"unrelated post-restart tail {i} " + ("T" * 80)}
            for i in range(20)
        ],
    }
    _text, trimmed = _prepare_tool_output_for_llm(
        "get_log_summary", json.dumps(noisy_summary), max_chars=700
    )
    assert trimmed is not None
    assert len(trimmed["session_recent_errors"]) == 1
    assert len(trimmed["session_tail_lines"]) == 1
    assert len(trimmed["post_session_lines"]) == 1
    assert len(trimmed["recent_errors"]) < 20


async def test_overview_with_many_missing_symbols_never_starves_logs_and_candidates(
    db_session: AsyncSession,
    storage_manager: StorageManager,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """Regression: 100+ non-crashing missing symbols must never starve logs or candidate_issues."""
    proj, crash, log_row = await _seed_crash_with_log(
        db_session, storage_manager, tmp_path, with_log=True
    )
    assert log_row is not None

    # Simulate a real Windows crash with register dumps + 110 missing OS/driver symbols.
    stacks = dict(crash.parsed_stacks_json or {})
    frames = list(stacks["threads"][0]["frames"])
    frames[0] = {
        **frames[0],
        "registers": {f"r{i}": f"0x{i:016x}" for i in range(16)},
    }
    stacks["threads"][0]["frames"] = frames
    crash.parsed_stacks_json = stacks
    crash.missing_symbols_json = [
        {
            "module_name": f"sysmod_{i:03d}.dll",
            "debug_file": f"sysmod_{i:03d}.pdb",
            "debug_id": f"{i:032X}1",
            "code_file": f"C:\\Windows\\System32\\sysmod_{i:03d}.dll",
            "code_id": f"6789{i:04X}",
        }
        for i in range(110)
    ]
    await db_session.commit()

    agent = AITriageAgent(storage=storage_manager, settings=test_settings)
    raw_overview = await agent._tool_get_crash_overview(
        db_session, crash, proj, await agent._resolve_runtime_config(db_session, None)
    )
    assert raw_overview["missing_symbol_count"] == 110
    assert raw_overview["crashing_stack_missing_symbol_count"] == 0
    assert len(raw_overview["missing_symbols"]) == 10
    assert "registers" not in raw_overview["top_frames"][0]
    assert len(raw_overview["logs"]) == 1
    assert raw_overview["logs"][0]["id"] == str(log_row.id)

    # Even if raw payload had 110 missing symbols directly, trimmer must still keep logs first.
    bloated_overview = {**raw_overview, "missing_symbols": list(crash.missing_symbols_json)}
    _delivered, trimmed = _prepare_tool_output_for_llm(
        "get_crash_overview", json.dumps(bloated_overview, default=str), max_chars=2500
    )
    assert trimmed is not None
    assert len(trimmed["logs"]) == 1
    assert trimmed["logs"][0]["id"] == str(log_row.id)
    assert len(trimmed["top_frames"]) >= 1

    # Log summary trimmer must keep post_session_lines and the most recent session_tail_lines.
    mega_summary = {
        "log_id": str(log_row.id),
        "filename": "runtime.log",
        "session_recent_errors": [],
        "session_tail_lines": [
            {"line_number": 100 + i, "text": f"older session line {i} " + ("A" * 120)}
            for i in range(25)
        ],
        "post_session_lines": [
            {"line_number": 126, "text": "FATAL Unhandled exception right after session"}
        ],
        "recent_errors": [],
        "tail_lines": [],
    }
    _del_sum, trimmed_sum = _prepare_tool_output_for_llm(
        "get_log_summary", json.dumps(mega_summary), max_chars=1200
    )
    assert trimmed_sum is not None
    assert len(trimmed_sum["post_session_lines"]) == 1
    assert len(trimmed_sum["session_tail_lines"]) >= 1
    # Suffix preservation: the line closest to the crash (line 124) must be kept!
    assert trimmed_sum["session_tail_lines"][-1]["line_number"] == 124
