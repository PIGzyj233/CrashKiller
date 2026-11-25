"""Comprehensive unit tests for deterministic clustering, fuzzy recall, and human verdicts."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CrashCluster, CrashReport, Issue, IssueStatusEvent, Project
from app.services.clustering import (
    ClusteringError,
    ClusteringService,
    _compare_versions,
    _trigram_similarity,
    _weighted_top10_jaccard,
    apply_human_verdict,
    cluster_crash,
    compute_signature_hash,
    filter_and_normalize_frames,
    normalize_function_name,
    recall_similar_issues,
    resolve_noise_rules,
)


def _api_key_hash(key: str = "secret") -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _make_stacks(
    frames: list[dict[str, Any]],
    crashing_thread_index: int = 0,
) -> dict[str, Any]:
    return {
        "crashing_thread_index": crashing_thread_index,
        "threads": [
            {
                "thread_index": crashing_thread_index,
                "thread_id": 1001,
                "thread_name": "MainThread",
                "frames": frames,
            }
        ],
        "modules": [],
    }


def _frame(
    module: str | None,
    function: str | None = None,
    *,
    module_offset: str | None = None,
    missing_symbols: bool = False,
    inlines: list[dict[str, Any]] | None = None,
    file: str | None = None,
    line: int | None = None,
) -> dict[str, Any]:
    return {
        "frame_index": 0,
        "ip": "0x7ff612340000",
        "module": module,
        "module_offset": module_offset,
        "function": function,
        "function_offset": "0x10",
        "file": file,
        "line": line,
        "trust": "cfi",
        "missing_symbols": missing_symbols,
        "inlines": inlines or [],
        "registers": {},
    }


async def _create_project(
    session: AsyncSession,
    *,
    key: str = "proj-a",
    settings_json: dict[str, Any] | None = None,
) -> Project:
    proj = Project(
        key=key,
        name=f"Project {key}",
        api_key_hash=_api_key_hash(),
        settings_json=settings_json or {},
    )
    session.add(proj)
    await session.flush()
    return proj


def test_noise_filtering_defaults_system_and_project_overrides() -> None:
    stacks = _make_stacks(
        [
            _frame(r"C:\Windows\System32\ntdll.dll", "KiUserExceptionDispatcher"),
            _frame("KERNELBASE.dll", "RaiseException"),
            _frame("ucrtbase.dll", "abort"),
            _frame("msvcp140.dll", "std::_Xlength_error(char const*)"),
            _frame("wrapper.dll", "CrashGuard::Forward"),
            _frame("custom_wrapper.dll", "MyHandler"),
            _frame(r"C:\Program Files\MyApp\Renderer.dll", "Renderer::DrawFrame(int)"),
        ]
    )

    # Default rules filter Windows/CRT noise but keep wrapper.dll & custom_wrapper.dll.
    default_frames = filter_and_normalize_frames(stacks)
    assert [f["module"] for f in default_frames] == [
        "wrapper.dll",
        "custom_wrapper.dll",
        "renderer.dll",
    ]

    # Layered system_settings + project.settings_json rules filter both wrappers.
    layered_rules = resolve_noise_rules(
        project_settings={
            "noise_modules": ["custom_wrapper.dll"],
            "custom_crash_handlers": ["TelemetryHook"],
        },
        system_settings={
            "default_noise_modules": [
                "ntdll.dll",
                "kernelbase.dll",
                "ucrtbase.dll",
                "msvcp140.dll",
                "wrapper.dll",
            ]
        },
    )
    stacks_with_hook = _make_stacks(
        [
            _frame("ntdll.dll", "KiUserExceptionDispatcher"),
            _frame("wrapper.dll", "CrashGuard::Forward"),
            _frame("custom_wrapper.dll", "MyHandler"),
            _frame("Renderer.dll", "TelemetryHook"),
            _frame("Renderer.dll", "Renderer::DrawFrame(int)"),
        ]
    )
    filtered = filter_and_normalize_frames(stacks_with_hook, layered_rules)
    assert [f["normalized"] for f in filtered] == ["renderer.dll!Renderer::DrawFrame(int)"]

    # replace_default_noise=True keeps ntdll.dll when only custom noise is specified.
    custom_only = resolve_noise_rules(
        project_settings={
            "replace_default_noise": True,
            "noise_modules": ["wrapper.dll"],
        }
    )
    kept = filter_and_normalize_frames(
        _make_stacks([_frame("ntdll.dll", "NtWait"), _frame("wrapper.dll", "Wrap")]),
        custom_only,
    )
    assert [f["module"] for f in kept] == ["ntdll.dll"]


def test_frame_normalization_offsets_lambdas_templates_and_inlines() -> None:
    assert (
        normalize_function_name("std::vector<int, std::allocator<int>>::at(unsigned long) + 0x20")
        == "std::vector<>::at(unsigned long)"
    )
    assert (
        normalize_function_name("Engine::Run::<lambda_a1b2c3d4e5>(int) + 16")
        == "Engine::Run::<lambda>(int)"
    )
    assert (
        normalize_function_name(
            "MyNS::Foo<int>::operator<<(std::ostream&, MyNS::Bar<double> const&) @ 0x7ff612341050"
        )
        == "MyNS::Foo<>::operator<<(std::ostream&, MyNS::Bar<> const&)"
    )
    assert normalize_function_name("Callback(0x7ff612341050)") == "Callback(<addr>)"
    assert (
        normalize_function_name("Node::operator->()")
        == "Node::operator->()"
    )
    assert (
        normalize_function_name("Item::operator<=(Item const&)")
        == "Item::operator<=(Item const&)"
    )

    # Inline expansion inherits parent module and matches non-inlined build identically.
    inlined_stack = _make_stacks(
        [
            _frame(
                "Engine.dll",
                "Engine::Tick()",
                inlines=[
                    {"function": "Engine::BindTexture(int)", "file": "tex.cpp", "line": 42},
                ],
            ),
            _frame("App.exe", "main"),
        ]
    )
    non_inlined_stack = _make_stacks(
        [
            _frame("Engine.dll", "Engine::BindTexture(int)", file="tex.cpp", line=42),
            _frame("Engine.dll", "Engine::Tick()"),
            _frame("App.exe", "main"),
        ]
    )
    norm_inlined = filter_and_normalize_frames(inlined_stack)
    norm_flat = filter_and_normalize_frames(non_inlined_stack)
    assert [f["normalized"] for f in norm_inlined] == [f["normalized"] for f in norm_flat]
    assert norm_inlined[0]["is_inline"] is True
    assert norm_inlined[0]["module"] == "engine.dll"

    pid = uuid.uuid4()
    assert compute_signature_hash(pid, "EXCEPTION_ACCESS_VIOLATION", norm_inlined) == (
        compute_signature_hash(pid, "EXCEPTION_ACCESS_VIOLATION", norm_flat)
    )

    # Missing-symbol frame uses module!module_offset (ASLR-stable) or module!unknown.
    missing_stack = _make_stacks(
        [
            _frame("Plugin.dll", None, module_offset="0x1A40", missing_symbols=True),
            _frame("Plugin.dll", None, module_offset=None, missing_symbols=True),
        ]
    )
    missing_norm = filter_and_normalize_frames(missing_stack)
    assert [f["normalized"] for f in missing_norm] == [
        "plugin.dll!0x1a40",
        "plugin.dll!unknown",
    ]


def test_fallback_when_zero_app_frames_remain() -> None:
    # Tier 1: only noise functions inside an app module -> keep non-noise-module frames.
    tier1 = filter_and_normalize_frames(
        _make_stacks([_frame("ntdll.dll", "KiUserExceptionDispatcher"), _frame("App.exe", "abort")])
    )
    assert [f["normalized"] for f in tier1] == ["app.exe!abort"]

    # Tier 2: all frames inside noise modules -> keep top 5 unfiltered frames.
    tier2 = filter_and_normalize_frames(
        _make_stacks(
            [
                _frame("ntdll.dll", f"NtFunc{i}")
                for i in range(7)
            ]
        )
    )
    assert len(tier2) == 5
    assert tier2[0]["normalized"] == "ntdll.dll!NtFunc0"

    # Tier 3: empty frames -> synthetic frame from crash_reason.
    tier3 = filter_and_normalize_frames(_make_stacks([]), crash_reason="EXCEPTION_STACK_OVERFLOW")
    assert tier3[0]["normalized"] == "unknown!EXCEPTION_STACK_OVERFLOW"
    sig = compute_signature_hash(uuid.uuid4(), "EXCEPTION_STACK_OVERFLOW", tier3)
    assert len(sig) == 64


def test_signature_hash_determinism_and_project_isolation() -> None:
    p1 = uuid.uuid4()
    p2 = uuid.uuid4()
    frames_a = filter_and_normalize_frames(
        _make_stacks(
            [
                _frame("ntdll.dll", "KiUserExceptionDispatcher"),
                _frame("Core.dll", "Core::Alloc(size_t) + 0x10"),
            ]
        )
    )
    frames_b = filter_and_normalize_frames(
        _make_stacks(
            [
                _frame("kernelbase.dll", "RaiseException"),
                _frame("Core.dll", "Core::Alloc(size_t) + 0x88"),
            ]
        )
    )

    h1 = compute_signature_hash(p1, "EXCEPTION_ACCESS_VIOLATION / 0x00000000", frames_a)
    h2 = compute_signature_hash(p1, "EXCEPTION_ACCESS_VIOLATION / 0xdeadbeef", frames_b)
    assert h1 == h2

    # Different projects and different exception types must not collide.
    assert compute_signature_hash(p2, "EXCEPTION_ACCESS_VIOLATION", frames_a) != h1
    assert compute_signature_hash(p1, "EXCEPTION_INT_DIVIDE_BY_ZERO", frames_a) != h1


def test_trigram_similarity_and_weighted_top10_jaccard() -> None:
    assert _trigram_similarity("RenderScene", "RenderScene") == pytest.approx(1.0)
    assert _trigram_similarity("", "RenderScene") == 0.0
    assert _trigram_similarity("Alpha", "Zebra") == 0.0
    assert 0.0 < _trigram_similarity("RenderSceneDraw", "RenderSceneTick") < 1.0

    base = [{"normalized": f"app.exe!Func{i}"} for i in range(6)]
    top_changed = [{"normalized": "app.exe!Different"}] + base[1:]
    deep_changed = base[:-1] + [{"normalized": "app.exe!Different"}]
    assert _weighted_top10_jaccard(base, deep_changed) > _weighted_top10_jaccard(base, top_changed)


async def test_recall_similar_issues_inline_shift_and_missing_symbols(
    db_session: AsyncSession,
) -> None:
    proj = await _create_project(db_session, key="recall-proj")
    other_proj = await _create_project(db_session, key="other-proj")

    issue_target = Issue(
        project_id=proj.id,
        title="Null deref in SubmitDrawCall",
        status="KNOWN",
        root_cause="Uninitialized texture pointer in SubmitDrawCall",
        crash_count=5,
    )
    issue_unrelated = Issue(
        project_id=proj.id,
        title="Audio mixer buffer underrun",
        status="KNOWN",
        root_cause="WASAPI stream closed unexpectedly",
        crash_count=20,
    )
    issue_other_proj = Issue(
        project_id=other_proj.id,
        title="Null deref in SubmitDrawCall",
        status="KNOWN",
        crash_count=99,
    )
    db_session.add_all([issue_target, issue_unrelated, issue_other_proj])
    await db_session.flush()

    target_frames = filter_and_normalize_frames(
        _make_stacks(
            [
                _frame("Renderer.dll", "SubmitDrawCall"),
                _frame("Renderer.dll", "RenderScene"),
                _frame("Engine.dll", "Tick"),
            ]
        )
    )
    unrelated_frames = filter_and_normalize_frames(
        _make_stacks(
            [
                _frame("Audio.dll", "MixChannels"),
                _frame("Audio.dll", "PumpDevice"),
            ]
        )
    )

    db_session.add_all(
        [
            CrashCluster(
                project_id=proj.id,
                issue_id=issue_target.id,
                signature_hash=compute_signature_hash(proj.id, "AV", target_frames),
                signature_frames_json=target_frames,
                signature_text=" ".join(f["normalized"] for f in target_frames),
                exception_type="AV",
                crash_count=5,
            ),
            CrashCluster(
                project_id=proj.id,
                issue_id=issue_unrelated.id,
                signature_hash=compute_signature_hash(proj.id, "AV", unrelated_frames),
                signature_frames_json=unrelated_frames,
                signature_text=" ".join(f["normalized"] for f in unrelated_frames),
                exception_type="AV",
                crash_count=20,
            ),
            CrashCluster(
                project_id=other_proj.id,
                issue_id=issue_other_proj.id,
                signature_hash=compute_signature_hash(other_proj.id, "AV", target_frames),
                signature_frames_json=target_frames,
                signature_text=" ".join(f["normalized"] for f in target_frames),
                exception_type="AV",
                crash_count=99,
            ),
        ]
    )
    await db_session.commit()

    # Inline shift adds a new top frame; issue_target must still rank #1 and never leak other_proj.
    shifted_frames = filter_and_normalize_frames(
        _make_stacks(
            [
                _frame("Renderer.dll", "InlineBindTexture"),
                _frame("Renderer.dll", "SubmitDrawCall"),
                _frame("Renderer.dll", "RenderScene"),
                _frame("Engine.dll", "Tick"),
            ]
        )
    )
    recalled_shift = await recall_similar_issues(db_session, proj.id, shifted_frames, limit=5)
    assert recalled_shift
    assert recalled_shift[0]["issue_id"] == issue_target.id
    assert all(item["issue_id"] != issue_other_proj.id for item in recalled_shift)

    # Partial missing symbol at frame 0 still recalls issue_target #1.
    partial_missing = filter_and_normalize_frames(
        _make_stacks(
            [
                _frame("Renderer.dll", None, module_offset="0x4020", missing_symbols=True),
                _frame("Renderer.dll", "RenderScene"),
                _frame("Engine.dll", "Tick"),
            ]
        )
    )
    recalled_missing = await recall_similar_issues(db_session, proj.id, partial_missing, limit=5)
    assert recalled_missing[0]["issue_id"] == issue_target.id


def test_postgresql_trigram_query_compiles() -> None:
    from sqlalchemy import func, select

    stmt = select(
        func.similarity(CrashCluster.signature_text, "app.exe!Main"),
        func.greatest(
            func.similarity(Issue.title, "Main crash"),
            func.similarity(func.coalesce(Issue.root_cause, ""), "Main crash"),
        ),
    )
    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "similarity(" in compiled
    assert "crash_clusters.signature_text" in compiled
    assert "issues.title" in compiled


async def test_cluster_crash_new_and_existing_syncs_issue_id(
    db_session: AsyncSession,
) -> None:
    proj = await _create_project(db_session, key="sync-proj")
    stacks = _make_stacks([_frame("App.exe", "CrashHere")])

    crash1 = CrashReport(
        project_id=proj.id,
        status="PARSING",
        app_version="1.0.0",
        crash_reason="EXCEPTION_ACCESS_VIOLATION",
        parsed_stacks_json=stacks,
    )
    db_session.add(crash1)
    await db_session.flush()

    res1 = await ClusteringService.cluster_crash(
        db_session, crash1, proj, triage_mode="SMART_TRIAGE"
    )
    assert res1.is_new_cluster is True
    assert res1.should_run_ai is True
    assert res1.should_trigger_ai is True
    assert res1.cluster.crash_count == 1
    assert crash1.cluster_id == res1.cluster.id

    issue = Issue(
        project_id=proj.id,
        title="Crash in CrashHere",
        status="KNOWN",
        is_ambiguous_stack=False,
        crash_count=1,
    )
    db_session.add(issue)
    await db_session.flush()
    res1.cluster.issue_id = issue.id
    crash1.issue_id = issue.id
    await db_session.commit()

    crash2 = CrashReport(
        project_id=proj.id,
        status="PARSING",
        app_version="1.0.1",
        crash_reason="EXCEPTION_ACCESS_VIOLATION",
        parsed_stacks_json=stacks,
        crash_time=datetime.now(UTC) + timedelta(minutes=5),
    )
    db_session.add(crash2)
    await db_session.flush()

    res2 = await cluster_crash(db_session, crash2, proj, triage_mode="SMART_TRIAGE")
    assert res2.is_new_cluster is False
    assert res2.should_run_ai is False
    assert res2.should_trigger_ai is False
    assert res2.trigger_reason == "known_unambiguous_issue"
    assert crash2.status == "ANALYZED"
    assert crash2.ai_verdict == "KNOWN_ISSUE"
    assert crash2.issue_id == issue.id
    assert res2.cluster.crash_count == 2
    assert issue.crash_count == 2


async def test_cluster_crash_triage_modes_ambiguous_and_regression(
    db_session: AsyncSession,
) -> None:
    proj = await _create_project(db_session, key="modes-proj")
    stacks = _make_stacks([_frame("Core.dll", "Core::Work")])

    fixed_issue = Issue(
        project_id=proj.id,
        title="Fixed bug",
        status="FIXED",
        fixed_in_version="1.2.0",
        is_ambiguous_stack=False,
        crash_count=1,
    )
    db_session.add(fixed_issue)
    await db_session.flush()

    norm = filter_and_normalize_frames(stacks)
    cluster = CrashCluster(
        project_id=proj.id,
        issue_id=fixed_issue.id,
        signature_hash=compute_signature_hash(proj.id, "AV", norm),
        signature_frames_json=norm,
        signature_text="core.dll!Core::Work",
        exception_type="AV",
        crash_count=1,
    )
    db_session.add(cluster)
    await db_session.commit()

    # Pre-fix build skips AI in SMART_TRIAGE.
    c_old = CrashReport(
        project_id=proj.id,
        status="PARSING",
        app_version="1.1.9",
        crash_reason="AV",
        parsed_stacks_json=stacks,
    )
    db_session.add(c_old)
    await db_session.flush()
    r_old = await cluster_crash(db_session, c_old, proj, triage_mode="SMART_TRIAGE")
    assert r_old.should_run_ai is False

    # Post-fix builds (including 1.10.0 > 1.2.0) must trigger AI regression check.
    for ver in ("1.2.5", "1.10.0"):
        c_new = CrashReport(
            project_id=proj.id,
            status="PARSING",
            app_version=ver,
            crash_reason="AV",
            parsed_stacks_json=stacks,
        )
        db_session.add(c_new)
        await db_session.flush()
        r_new = await cluster_crash(db_session, c_new, proj, triage_mode="SMART_TRIAGE")
        assert r_new.should_run_ai is True
        assert r_new.trigger_reason == "potential_regression"

    # ALWAYS triggers AI even on pre-fix known issue; MANUAL skips AI and leaves ai_verdict=None.
    c_always = CrashReport(
        project_id=proj.id,
        status="PARSING",
        app_version="1.1.9",
        crash_reason="AV",
        parsed_stacks_json=stacks,
    )
    c_manual = CrashReport(
        project_id=proj.id,
        status="PARSING",
        app_version="1.2.5",
        crash_reason="AV",
        parsed_stacks_json=stacks,
    )
    db_session.add_all([c_always, c_manual])
    await db_session.flush()

    res_always = await cluster_crash(db_session, c_always, proj, triage_mode="ALWAYS")
    assert res_always.should_run_ai is True
    res_manual = await cluster_crash(db_session, c_manual, proj, triage_mode="MANUAL")
    assert res_manual.should_run_ai is False
    assert c_manual.status == "ANALYZED"
    assert c_manual.ai_verdict is None

    # Ambiguous stack triggers AI in SMART_TRIAGE even on pre-fix build.
    fixed_issue.is_ambiguous_stack = True
    c_ambig = CrashReport(
        project_id=proj.id,
        status="PARSING",
        app_version="1.1.9",
        crash_reason="AV",
        parsed_stacks_json=stacks,
    )
    db_session.add(c_ambig)
    await db_session.flush()
    r_ambig = await cluster_crash(db_session, c_ambig, proj, triage_mode="SMART_TRIAGE")
    assert r_ambig.should_run_ai is True
    assert r_ambig.trigger_reason == "ambiguous_stack"


async def test_cluster_crash_idempotent_on_reprocess_and_resymbolicate(
    db_session: AsyncSession,
) -> None:
    proj = await _create_project(db_session, key="reproc-proj")
    unresolved = _make_stacks(
        [_frame("Core.dll", None, module_offset="0x1000", missing_symbols=True)]
    )
    resolved = _make_stacks([_frame("Core.dll", "Core::AllocateBuffer(size_t)")])

    crash = CrashReport(
        project_id=proj.id,
        status="PARSING",
        app_version="1.0.0",
        crash_reason="AV",
        parsed_stacks_json=unresolved,
    )
    db_session.add(crash)
    await db_session.flush()

    r1 = await cluster_crash(db_session, crash, proj)
    old_cluster = r1.cluster
    assert old_cluster.crash_count == 1

    # Re-clustering same stack must be idempotent.
    r1_again = await cluster_crash(db_session, crash, proj)
    assert r1_again.cluster.id == old_cluster.id
    assert old_cluster.crash_count == 1

    # Resymbolicating changes signature_hash, decrements old cluster, increments new cluster.
    crash.parsed_stacks_json = resolved
    r2 = await cluster_crash(db_session, crash, proj)
    assert r2.signature_hash != r1.signature_hash
    assert r2.cluster.id != old_cluster.id
    assert old_cluster.crash_count == 0
    assert r2.cluster.crash_count == 1


async def test_resymbolication_preserves_human_assignment(db_session: AsyncSession) -> None:
    project = await _create_project(db_session, key="human-resymbolication")
    crash = CrashReport(
        project_id=project.id,
        status="ANALYZED",
        app_version="1.0.0",
        crash_reason="AV",
        parsed_stacks_json=_make_stacks(
            [_frame("Core.dll", None, module_offset="0x1000", missing_symbols=True)]
        ),
    )
    db_session.add(crash)
    original = await cluster_crash(db_session, crash, project)
    await apply_human_verdict(
        db_session, crash, action="create_issue", new_issue_data={"title": "Reviewed"},
    )
    reviewed_issue_id = crash.issue_id

    crash.parsed_stacks_json = _make_stacks([_frame("Core.dll", "Core::AllocateBuffer")])
    result = await cluster_crash(db_session, crash, project, triage_mode="ALWAYS")

    assert result.cluster.id != original.cluster.id
    assert reviewed_issue_id is not None and crash.issue_id == reviewed_issue_id
    assert crash.is_human_verified and not result.should_run_ai
    assert result.issue is not None and result.issue.crash_count == 1


async def test_apply_human_verdict_confirm_assign_and_create_issue(
    db_session: AsyncSession,
) -> None:
    proj = await _create_project(db_session, key="verdict-proj")
    other_proj = await _create_project(db_session, key="other-verdict-proj")
    stacks = _make_stacks([_frame("App.exe", "DoWork")])

    crash = CrashReport(
        project_id=proj.id,
        status="ANALYZED",
        app_version="1.0.0",
        crash_reason="AV",
        parsed_stacks_json=stacks,
    )
    db_session.add(crash)
    await db_session.flush()
    res = await cluster_crash(db_session, crash, proj)
    cluster = res.cluster

    # create_issue creates Issue, links crash & cluster, sets is_human_verified=True.
    await apply_human_verdict(
        db_session,
        crash,
        action="create_issue",
        new_issue_data={"title": "DoWork null ptr", "component": "Core", "status": "NEW"},
        root_cause="Missing null check",
        add_log_patterns=["NullPtrException", "NullPtrException"],
    )
    assert crash.is_human_verified is True
    assert crash.issue_id is not None
    assert cluster.issue_id == crash.issue_id
    created_issue = await db_session.get(Issue, crash.issue_id)
    assert created_issue is not None
    assert created_issue.root_cause == "Missing null check"
    assert created_issue.log_patterns_json == ["NullPtrException"]
    assert created_issue.crash_count == 1

    # confirm promotes NEW -> KNOWN and deduplicates new log patterns.
    await apply_human_verdict(
        db_session,
        crash,
        action="confirm",
        add_log_patterns=["NullPtrException", "FatalAbort"],
    )
    await db_session.refresh(created_issue)
    assert created_issue.status == "KNOWN"
    assert created_issue.log_patterns_json == ["NullPtrException", "FatalAbort"]

    verdict_events = (
        await db_session.execute(
            select(IssueStatusEvent)
            .where(IssueStatusEvent.issue_id == created_issue.id)
            .order_by(IssueStatusEvent.id.asc())
        )
    ).scalars().all()
    assert any(
        ev.from_status == "NEW"
        and ev.to_status == "KNOWN"
        and ev.source == "human_verdict"
        and ev.crash_id == crash.id
        for ev in verdict_events
    )

    # assign_issue moves crash to another issue and decrements old issue; ambiguous unbinds cluster.
    target_issue = Issue(project_id=proj.id, title="Target Issue", status="KNOWN", crash_count=0)
    foreign_issue = Issue(project_id=other_proj.id, title="Foreign", status="KNOWN", crash_count=0)
    db_session.add_all([target_issue, foreign_issue])
    await db_session.commit()

    with pytest.raises(ClusteringError):
        await apply_human_verdict(
            db_session, crash, action="assign_issue", issue_id=foreign_issue.id
        )

    await apply_human_verdict(
        db_session,
        crash,
        action="assign_issue",
        issue_id=target_issue.id,
        is_ambiguous_stack=True,
    )
    await db_session.refresh(created_issue)
    await db_session.refresh(target_issue)
    await db_session.refresh(cluster)
    assert crash.issue_id == target_issue.id
    assert created_issue.crash_count == 0
    assert target_issue.crash_count == 1
    assert target_issue.is_ambiguous_stack is True
    assert cluster.issue_id is None


async def test_clustering_hardening_templates_versions_paths_and_verdicts(
    db_session: AsyncSession,
) -> None:
    # 1. Non-type template arguments with <<, >>, <=, >=, and > inside/after (...).
    assert normalize_function_name("Bitset<1 << 3>::Test()") == "Bitset<>::Test()"
    assert normalize_function_name("Bitset<8 >> 1>::Test()") == "Bitset<>::Test()"
    assert (
        normalize_function_name("std::enable_if<sizeof(T) > 4, void>::type Foo<int>()")
        == "std::enable_if<>::type Foo<>()"
    )
    assert (
        normalize_function_name("std::enable_if<(sizeof(T) > 4), void>::type Foo<int>()")
        == "std::enable_if<>::type Foo<>()"
    )
    assert (
        normalize_function_name("std::enable_if<sizeof(T) >= 4 && 1 <= 2, void>::type Bar<int>()")
        == "std::enable_if<>::type Bar<>()"
    )
    assert (
        normalize_function_name("std::enable_if<sizeof(T) < 4, void>::type Foo<int>()")
        == "std::enable_if<>::type Foo<>()"
    )
    assert (
        normalize_function_name("std::enable_if<4 < N, void>::type Foo<int>()")
        == "std::enable_if<>::type Foo<>()"
    )
    assert normalize_function_name("Bitset<1 < 3>::Test()") == "Bitset<>::Test()"
    assert normalize_function_name("Outer<Bitset<1 < 3>>::Test()") == "Outer<>::Test()"
    assert (
        normalize_function_name(
            "std::enable_if<sizeof(T) > 4 && sizeof(U) < 2, void>::type Foo<int>()"
        )
        == "std::enable_if<>::type Foo<>()"
    )
    assert normalize_function_name("std::vector<std::pair<int, int>>") == "std::vector<>"

    # 2. Trailing backslash/slash module path and malformed non-dict thread item.
    trailing_norm = filter_and_normalize_frames(
        _make_stacks([_frame(r"C:\Dir\Mod.dll\\", "Mod::Run()")])
    )
    assert trailing_norm[0]["module"] == "mod.dll"
    assert trailing_norm[0]["normalized"] == "mod.dll!Mod::Run()"

    malformed_norm = filter_and_normalize_frames(
        {"crashing_thread_index": 0, "threads": [None]},  # type: ignore[list-item]
        crash_reason="EXCEPTION_ACCESS_VIOLATION",
    )
    assert malformed_norm[0]["normalized"] == "unknown!EXCEPTION_ACCESS_VIOLATION"

    # 3. Pre-release version comparison vs release and numeric major/minor.
    assert _compare_versions("1.2.0-rc1", "1.2.0") < 0
    assert _compare_versions("1.2.0-alpha", "1.2.0-beta") < 0
    assert _compare_versions("1.2.0-beta", "1.2.0-rc1") < 0
    assert _compare_versions("1.2.0-rc2", "1.2.0-rc10") < 0
    assert _compare_versions("1.10.0", "1.2.0") > 0
    assert _compare_versions("1.2.5", "1.2.0") > 0

    proj = await _create_project(db_session, key="harden-proj")
    stacks = _make_stacks([_frame("App.exe", "RunStep")])
    norm = filter_and_normalize_frames(stacks)

    fixed_issue = Issue(
        project_id=proj.id,
        title="Fixed in 1.2.0",
        status="FIXED",
        fixed_in_version="1.2.0",
        is_ambiguous_stack=False,
        crash_count=1,
    )
    ambig_issue = Issue(
        project_id=proj.id,
        title="Pre-existing ambiguous issue",
        status="KNOWN",
        is_ambiguous_stack=True,
        crash_count=0,
    )
    db_session.add_all([fixed_issue, ambig_issue])
    await db_session.flush()

    cluster = CrashCluster(
        project_id=proj.id,
        issue_id=fixed_issue.id,
        signature_hash=compute_signature_hash(proj.id, "AV", norm),
        signature_frames_json=norm,
        signature_text="app.exe!RunStep",
        exception_type="AV",
        crash_count=1,
    )
    db_session.add(cluster)
    await db_session.commit()

    # Pre-release 1.2.0-rc1 is strictly before 1.2.0 -> must NOT trigger potential_regression.
    c_rc = CrashReport(
        project_id=proj.id,
        status="PARSING",
        app_version="1.2.0-rc1",
        crash_reason="AV",
        parsed_stacks_json=stacks,
    )
    db_session.add(c_rc)
    await db_session.flush()
    res_rc = await cluster_crash(db_session, c_rc, proj, triage_mode="SMART_TRIAGE")
    assert res_rc.should_run_ai is False
    assert res_rc.trigger_reason == "known_unambiguous_issue"

    # 4. Invalid status in create_issue raises ClusteringError.
    with pytest.raises(ClusteringError):
        await apply_human_verdict(
            db_session,
            c_rc,
            action="create_issue",
            new_issue_data={"title": "Bad status", "status": "BOGUS"},
        )

    # 5. assign_issue to pre-existing is_ambiguous_stack=True with is_ambiguous_stack=None
    # must clear stale cluster.issue_id.
    await apply_human_verdict(
        db_session,
        c_rc,
        action="assign_issue",
        issue_id=ambig_issue.id,
        is_ambiguous_stack=None,
    )
    await db_session.refresh(cluster)
    await db_session.refresh(c_rc)
    assert c_rc.issue_id == ambig_issue.id
    assert cluster.issue_id is None

    # 6. create_issue with whitespace-only component normalizes to None
    await apply_human_verdict(
        db_session,
        c_rc,
        action="create_issue",
        new_issue_data={"title": "Clean Comp", "component": "   \t  "},
    )
    await db_session.refresh(c_rc)
    assert c_rc.issue_id is not None
    new_iss = await db_session.get(Issue, c_rc.issue_id)
    assert new_iss is not None
    assert new_iss.component is None
