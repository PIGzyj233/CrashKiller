"""Idempotent demo data seed script for CrashKiller."""

from __future__ import annotations

import asyncio
import io
import sys
import uuid
from datetime import timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select  # noqa: E402

from app.api.deps import hash_api_key  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.base import Base, utcnow  # noqa: E402
from app.db.models import (  # noqa: E402
    AIAnalysisRun,
    CrashCluster,
    CrashLog,
    CrashReport,
    Issue,
    Project,
    Symbol,
)
from app.db.session import dispose_engine, get_session_factory, init_db  # noqa: E402
from app.services.clustering import (  # noqa: E402
    compute_signature_hash,
    filter_and_normalize_frames,
)
from app.services.log_engine import LogEngine  # noqa: E402
from app.services.storage import StorageManager  # noqa: E402

SEED_NAMESPACE = uuid.UUID("9e7c5a10-4b21-4c9a-9f88-123456789abc")


def _det_uuid(name: str) -> uuid.UUID:
    return uuid.uuid5(SEED_NAMESPACE, name)


async def seed_demo_data() -> None:
    """Populate idempotent demo-game project, symbols, issues, clusters, crashes, and logs."""
    get_settings.cache_clear()
    settings = get_settings()
    engine = init_db(settings.DATABASE_URL, echo=settings.DEBUG)

    try:
        if "sqlite" in settings.DATABASE_URL.lower():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        storage = StorageManager(data_dir=settings.DATA_DIR, settings=settings)
        log_engine = LogEngine()
        session_factory = get_session_factory()
        now = utcnow()

        async with session_factory() as session:
            proj = (
                await session.execute(select(Project).where(Project.key == "demo-game"))
            ).scalar_one_or_none()
            if proj is None:
                proj = Project(
                    id=_det_uuid("project:demo-game"),
                    key="demo-game",
                    name="Demo Game Engine",
                    api_key_hash=hash_api_key("ck_demo_game_seed_key_123456"),
                    settings_json={
                        "ai_triage_mode": "SMART_TRIAGE",
                        "noise_modules": ["ntdll.dll", "kernel32.dll"],
                    },
                    created_at=now - timedelta(days=7),
                )
                session.add(proj)
                await session.flush()

            # 1. Symbols (idempotent per (project_id, debug_file, debug_id))
            sym_specs = [
                {
                    "app_version": "1.4.0",
                    "module_name": "GameRender",
                    "debug_file": "GameRender.pdb",
                    "debug_id": "DEMO00011111222233334444555566661",
                    "code_file": "GameRender.dll",
                    "code_id": "6500AA11",
                    "has_pe_unwind": True,
                },
                {
                    "app_version": "1.4.2",
                    "module_name": "GameNet",
                    "debug_file": "GameNet.pdb",
                    "debug_id": "DEMO00021111222233334444555566661",
                    "code_file": "GameNet.dll",
                    "code_id": "6500BB22",
                    "has_pe_unwind": True,
                },
                {
                    "app_version": "1.4.2",
                    "module_name": "AudioPlugin",
                    "debug_file": "AudioPlugin.pdb",
                    "debug_id": "DEMO00031111222233334444555566661",
                    "code_file": "AudioPlugin.dll",
                    "code_id": "6500CC33",
                    "has_pe_unwind": False,
                },
            ]
            for spec in sym_specs:
                existing_sym = (
                    await session.execute(
                        select(Symbol).where(
                            Symbol.project_id == proj.id,
                            Symbol.debug_file == spec["debug_file"],
                            Symbol.debug_id == spec["debug_id"],
                        )
                    )
                ).scalar_one_or_none()
                if existing_sym is None:
                    stem = Path(spec["debug_file"]).stem
                    rel_sym = (
                        f"symbols/sym/{proj.id}/{spec['debug_file']}/{spec['debug_id']}/{stem}.sym"
                    )
                    sym_bytes = (
                        f"MODULE windows x86_64 {spec['debug_id']} {spec['debug_file']}\n"
                        f"INFO CODE_ID {spec['code_id']} {spec['code_file']}\n"
                    ).encode()
                    storage.atomic_write_bytes(rel_sym, sym_bytes)
                    session.add(
                        Symbol(
                            id=_det_uuid(f"symbol:{spec['debug_id']}"),
                            project_id=proj.id,
                            app_version=spec["app_version"],
                            module_name=spec["module_name"],
                            debug_file=spec["debug_file"],
                            debug_id=spec["debug_id"],
                            code_file=spec["code_file"],
                            code_id=spec["code_id"],
                            has_pe_unwind=spec["has_pe_unwind"],
                            sym_path=rel_sym,
                            sym_size=len(sym_bytes),
                            raw_archive_path=None,
                            raw_size=0,
                            last_used_at=now - timedelta(hours=2),
                            created_at=now - timedelta(days=5),
                        )
                    )

            # 2. Check if demo crashes already seeded (idempotency guard)
            c1_id = _det_uuid("crash:1")
            if await session.get(CrashReport, c1_id) is not None:
                await session.commit()
                return

            # 3. Issues covering NEW, KNOWN, FIXED, REGRESSION
            issue_known = Issue(
                project_id=proj.id,
                title="Null texture descriptor dereference in Renderer::SubmitFrame",
                status="KNOWN",
                component="Rendering",
                root_cause="Async streaming freed texture handle before command list flush.",
                log_patterns_json=["DX12_ERR_DEVICE_REMOVED", "TextureHandle invalid"],
                is_ambiguous_stack=False,
                is_pinned=True,
                crash_count=2,
                first_seen_at=now - timedelta(days=5),
                last_seen_at=now - timedelta(hours=3),
            )
            issue_new = Issue(
                project_id=proj.id,
                title="Packet buffer overflow in NetSession::ProcessSnapshot",
                status="NEW",
                component="Networking",
                root_cause="Unbounded delta frame count from unreliable peer packet.",
                log_patterns_json=["NET_SNAPSHOT_OVERFLOW"],
                is_ambiguous_stack=False,
                is_pinned=False,
                crash_count=1,
                first_seen_at=now - timedelta(days=2),
                last_seen_at=now - timedelta(days=2),
            )
            issue_fixed = Issue(
                project_id=proj.id,
                title="Race condition in AudioMixer::Shutdown",
                status="FIXED",
                component="Audio",
                root_cause="Worker thread accessed mixer ring buffer after teardown.",
                log_patterns_json=["AUDIO_RING_SHUTDOWN"],
                fixed_in_version="1.4.0",
                is_ambiguous_stack=False,
                is_pinned=False,
                crash_count=0,
                first_seen_at=now - timedelta(days=10),
                last_seen_at=now - timedelta(days=8),
            )
            issue_regression = Issue(
                project_id=proj.id,
                title="Deadlock regression in AssetLoader::FlushQueue",
                status="REGRESSION",
                component="CoreIO",
                root_cause="Lock order inversion re-introduced in 1.4.2.",
                log_patterns_json=["ASSET_FLUSH_DEADLOCK"],
                fixed_in_version="1.4.0",
                is_ambiguous_stack=False,
                is_pinned=False,
                crash_count=1,
                first_seen_at=now - timedelta(days=6),
                last_seen_at=now - timedelta(hours=6),
            )
            for iss in (issue_known, issue_new, issue_fixed, issue_regression):
                session.add(iss)
            await session.flush()

            # 4. Stacks, Clusters, Crashes, Logs & AIAnalysisRuns
            crashes_spec = [
                {
                    "id": c1_id,
                    "log_id": _det_uuid("log:1"),
                    "run_id": _det_uuid("run:1"),
                    "issue": issue_known,
                    "verdict": "KNOWN_ISSUE",
                    "confidence": 0.96,
                    "app_version": "1.4.2",
                    "reason": "EXCEPTION_ACCESS_VIOLATION_READ",
                    "address": "0x0000000000000018",
                    "module": "GameRender.dll",
                    "func": "Renderer::SubmitFrame(RenderContext&)",
                    "file": "src/render/frame.cpp",
                    "line": 214,
                    "missing": [],
                    "is_pinned": True,
                    "is_verified": True,
                    "age_hours": 3,
                    "session_id": "sess-demo-render-001",
                    "log_lines": [
                        (
                            "2026-11-22 09:55:00.000 INFO [sess-demo-prev-000] "
                            "Previous session started"
                        ),
                        (
                            "2026-11-22 09:59:50.000 INFO [sess-demo-prev-000] "
                            "Previous session ended cleanly"
                        ),
                        "2026-11-22 10:00:00.000 INFO [sess-demo-render-001] Engine boot v1.4.2",
                        (
                            "2026-11-22 10:00:05.120 WARN [sess-demo-render-001] "
                            "Texture cache eviction pressure high"
                        ),
                        (
                            "2026-11-22 10:00:08.450 ERROR [sess-demo-render-001] "
                            "TextureHandle invalid for slot 4"
                        ),
                        (
                            "2026-11-22 10:00:08.900 FATAL [sess-demo-render-001] "
                            "DX12_ERR_DEVICE_REMOVED during SubmitFrame"
                        ),
                        (
                            "2026-11-22 10:00:08.950 FATAL Unhandled exception 0xC0000005, "
                            "writing minidump"
                        ),
                        "2026-11-22 10:00:12.000 INFO === Process auto-restarted (pid=8842) ===",
                        (
                            "2026-11-22 10:01:00.000 INFO [sess-demo-after-002] "
                            "New session started after restart"
                        ),
                        (
                            "2026-11-22 10:05:00.000 ERROR [sess-demo-after-002] "
                            "Unrelated post-restart transient network timeout"
                        ),
                    ],
                },
                {
                    "id": _det_uuid("crash:2"),
                    "log_id": _det_uuid("log:2"),
                    "run_id": _det_uuid("run:2"),
                    "issue": issue_known,
                    "verdict": "KNOWN_ISSUE",
                    "confidence": 0.92,
                    "app_version": "1.4.1",
                    "reason": "EXCEPTION_ACCESS_VIOLATION_READ",
                    "address": "0x0000000000000018",
                    "module": "GameRender.dll",
                    "func": "Renderer::SubmitFrame(RenderContext&)",
                    "file": "src/render/frame.cpp",
                    "line": 214,
                    "missing": [],
                    "is_pinned": False,
                    "is_verified": False,
                    "age_hours": 28,
                    "session_id": "sess-demo-render-001",
                    "log_lines": [
                        "2026-11-21 09:10:00.000 INFO [sess-demo-render-001] Engine boot v1.4.1",
                        (
                            "2026-11-21 09:10:03.220 ERROR [sess-demo-render-001] "
                            "TextureHandle invalid for slot 2"
                        ),
                        (
                            "2026-11-21 09:10:03.500 FATAL [sess-demo-render-001] "
                            "DX12_ERR_DEVICE_REMOVED during SubmitFrame"
                        ),
                    ],
                },
                {
                    "id": _det_uuid("crash:3"),
                    "log_id": _det_uuid("log:3"),
                    "run_id": _det_uuid("run:3"),
                    "issue": issue_new,
                    "verdict": "NEW_ISSUE",
                    "confidence": 0.89,
                    "app_version": "1.4.2",
                    "reason": "EXCEPTION_STACK_BUFFER_OVERRUN",
                    "address": "0x00007ff600124400",
                    "module": "GameNet.dll",
                    "func": "NetSession::ProcessSnapshot(PacketReader&)",
                    "file": "src/net/session.cpp",
                    "line": 412,
                    "missing": [
                        {
                            "module_name": "ThirdPartyPhysics.dll",
                            "debug_file": "ThirdPartyPhysics.pdb",
                            "debug_id": "9999AAAA1111BBBB2222CCCC3333DDDD1",
                            "code_file": "ThirdPartyPhysics.dll",
                            "code_id": "6500DD44",
                        }
                    ],
                    "is_pinned": False,
                    "is_verified": False,
                    "age_hours": 48,
                    "log_lines": [
                        "2026-11-20 14:20:00.000 INFO Connected to relay server",
                        "2026-11-20 14:20:11.010 WARN 渲染管线同步延迟 34ms",
                        "2026-11-20 14:20:12.400 ERROR NET_SNAPSHOT_OVERFLOW delta_count=4096",
                    ],
                },
                {
                    "id": _det_uuid("crash:4"),
                    "log_id": _det_uuid("log:4"),
                    "run_id": _det_uuid("run:4"),
                    "issue": issue_regression,
                    "verdict": "REGRESSION",
                    "confidence": 0.91,
                    "app_version": "1.4.2",
                    "reason": "EXCEPTION_BREAKPOINT",
                    "address": "0x00007ff600338810",
                    "module": "GameCore.dll",
                    "func": "AssetLoader::FlushQueue()",
                    "file": "src/core/asset_loader.cpp",
                    "line": 159,
                    "missing": [],
                    "is_pinned": False,
                    "is_verified": False,
                    "age_hours": 6,
                    "log_lines": [
                        "2026-11-22 07:00:00.000 INFO Loading level_04",
                        "2026-11-22 07:00:15.100 ERROR ASSET_FLUSH_DEADLOCK watchdog timeout",
                    ],
                },
            ]

            clusters_by_sig: dict[str, CrashCluster] = {}

            for spec in crashes_spec:
                cid: uuid.UUID = spec["id"]
                crash_ts = now - timedelta(hours=int(spec["age_hours"]))
                parsed_stacks = {
                    "crash_reason": spec["reason"],
                    "crash_address": spec["address"],
                    "os_version": "Windows 11 23H2",
                    "cpu_arch": "amd64",
                    "crashing_thread_index": 0,
                    "threads": [
                        {
                            "thread_index": 0,
                            "thread_name": "MainThread",
                            "is_crashing": True,
                            "registers": {"rip": spec["address"], "rsp": "0x000000b1a23ff780"},
                            "frames": [
                                {
                                    "frame_index": 0,
                                    "module": spec["module"],
                                    "function": spec["func"],
                                    "file": spec["file"],
                                    "line": spec["line"],
                                    "trust": "cfi",
                                    "is_noise": False,
                                    "inlines": [],
                                },
                                {
                                    "frame_index": 1,
                                    "module": "GameCore.dll",
                                    "function": "EngineLoop::Tick(float)",
                                    "file": "src/core/engine_loop.cpp",
                                    "line": 94,
                                    "trust": "cfi",
                                    "is_noise": False,
                                    "inlines": [],
                                },
                                {
                                    "frame_index": 2,
                                    "module": "ntdll.dll",
                                    "function": "RtlUserThreadStart",
                                    "file": None,
                                    "line": None,
                                    "trust": "context",
                                    "is_noise": True,
                                    "inlines": [],
                                },
                            ],
                        }
                    ],
                    "modules": [
                        {
                            "filename": spec["module"],
                            "debug_file": spec["module"].replace(".dll", ".pdb"),
                            "debug_id": "DEMO00011111222233334444555566661",
                            "loaded_symbols": True,
                            "missing_symbols": False,
                        }
                    ],
                    "missing_symbols": spec["missing"],
                }
                parsed_summary = {
                    "crash_reason": spec["reason"],
                    "crash_address": spec["address"],
                    "os_version": "Windows 11 23H2",
                    "cpu_arch": "amd64",
                    "crashing_thread_index": 0,
                    "top_frames": parsed_stacks["threads"][0]["frames"][:2],
                    "missing_symbol_count": len(spec["missing"]),
                    "thread_count": 1,
                    "module_count": 1,
                }

                norm_frames = filter_and_normalize_frames(
                    parsed_stacks,
                    crash_reason=spec["reason"],
                )
                sig_hash = compute_signature_hash(proj.id, spec["reason"], norm_frames)
                cluster = clusters_by_sig.get(sig_hash)
                if cluster is None:
                    cluster = (
                        await session.execute(
                            select(CrashCluster).where(CrashCluster.signature_hash == sig_hash)
                        )
                    ).scalar_one_or_none()
                if cluster is None:
                    cluster = CrashCluster(
                        id=_det_uuid(f"cluster:{sig_hash}"),
                        project_id=proj.id,
                        issue_id=spec["issue"].id,
                        signature_hash=sig_hash,
                        signature_frames_json=norm_frames,
                        signature_text=" | ".join(
                            f"{f.get('module')}!{f.get('function')}" for f in norm_frames
                        ),
                        exception_type=spec["reason"],
                        crash_count=1,
                    )
                    session.add(cluster)
                    await session.flush()
                else:
                    cluster.crash_count += 1
                clusters_by_sig[sig_hash] = cluster

                dump_payload = (
                    b"MDMP\x93\xa7\x00\x00" + f"SEED_DUMP_{cid}".encode()
                )
                dump_rel, dump_raw_sz, dump_comp_sz = storage.compress_dump(
                    io.BytesIO(dump_payload), cid
                )

                crash = CrashReport(
                    id=cid,
                    project_id=proj.id,
                    cluster_id=cluster.id,
                    issue_id=spec["issue"].id,
                    status="ANALYZED",
                    app_version=spec["app_version"],
                    build_id="demo-build-42",
                    os_version="Windows 11 23H2",
                    session_id=spec.get("session_id"),
                    dump_path=dump_rel,
                    dump_raw_size=dump_raw_sz,
                    dump_compressed_size=dump_comp_sz,
                    crash_reason=spec["reason"],
                    crash_address=spec["address"],
                    parsed_summary_json=parsed_summary,
                    parsed_stacks_json=parsed_stacks,
                    missing_symbols_json=spec["missing"],
                    ai_verdict=spec["verdict"],
                    ai_confidence=spec["confidence"],
                    is_human_verified=spec["is_verified"],
                    is_pinned=spec["is_pinned"],
                    crash_time=crash_ts,
                    created_at=crash_ts,
                )
                session.add(crash)
                await session.flush()

                log_id: uuid.UUID = spec["log_id"]
                rel_zst = f"logs/{cid}/{log_id}.log.zst"
                rel_idx = f"logs/{cid}/{log_id}.idx.json"
                zst_abs = storage.resolve_path(rel_zst)
                idx_abs = storage.resolve_path(rel_idx)
                zst_abs.parent.mkdir(parents=True, exist_ok=True)
                log_bytes = ("\n".join(spec["log_lines"]) + "\n").encode("utf-8")
                log_res = log_engine.ingest_log(
                    io.BytesIO(log_bytes),
                    zst_abs,
                    idx_abs,
                    chunk_size_bytes=settings.LOG_CHUNK_SIZE_BYTES,
                    zstd_level=settings.LOG_ZSTD_LEVEL,
                    session_id=spec.get("session_id"),
                )
                session.add(
                    CrashLog(
                        id=log_id,
                        crash_id=cid,
                        filename="game_runtime.log",
                        zst_path=rel_zst,
                        idx_path=rel_idx,
                        raw_size=int(log_res["raw_size"]),
                        compressed_size=int(log_res["compressed_size"]),
                        total_lines=int(log_res["total_lines"]),
                        digest_json=dict(log_res["digest_json"]),
                    )
                )

                session.add(
                    AIAnalysisRun(
                        id=spec["run_id"],
                        crash_id=cid,
                        model_name=settings.LLM_MODEL,
                        status="COMPLETED",
                        result_json={
                            "verdict": spec["verdict"],
                            "matched_issue_id": spec["issue"].id,
                            "confidence": spec["confidence"],
                            "suggested_title": spec["issue"].title,
                            "root_cause_analysis": spec["issue"].root_cause or "",
                            "reasoning_summary": (
                                f"Matched top stack frame {spec['func']} and runtime log markers."
                            ),
                            "evidence_log_snippets": [
                                {
                                    "log_id": str(log_id),
                                    "start_line": 1,
                                    "end_line": len(spec["log_lines"]),
                                    "relevance": "Runtime error sequence preceding crash",
                                }
                            ],
                            "key_log_patterns": list(spec["issue"].log_patterns_json),
                            "suggested_component": spec["issue"].component,
                        },
                        trace_json=[
                            {
                                "step": 1,
                                "role": "assistant",
                                "tool_name": "get_crash_overview",
                                "arguments": {"crash_id": str(cid)},
                                "output_preview": f"{spec['reason']} at {spec['func']}",
                                "duration_ms": 14,
                            },
                            {
                                "step": 2,
                                "role": "assistant",
                                "tool_name": "read_log_lines",
                                "arguments": {
                                    "log_id": str(log_id),
                                    "start_line": 1,
                                    "end_line": len(spec["log_lines"]),
                                },
                                "output_preview": spec["log_lines"][-1],
                                "duration_ms": 19,
                            },
                        ],
                        prompt_tokens=420,
                        completion_tokens=180,
                        duration_ms=850,
                        created_at=crash_ts + timedelta(seconds=5),
                    )
                )

            await session.commit()
    finally:
        await dispose_engine()


def main() -> int:
    asyncio.run(seed_demo_data())
    print("Seeded CrashKiller demo-game data successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
