"""Database ORM models for CrashKiller."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONBType, TZDateTime, UUIDType, utcnow


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    api_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSONBType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)

    symbols: Mapped[list[Symbol]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    issues: Mapped[list[Issue]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    clusters: Mapped[list[CrashCluster]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    crash_reports: Mapped[list[CrashReport]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Symbol(Base):
    __tablename__ = "symbols"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "debug_file",
            "debug_id",
            name="uq_symbols_project_debug_file_id",
        ),
        Index("ix_symbols_debug_file_debug_id", "debug_file", "debug_id"),
        Index("ix_symbols_code_file_code_id", "code_file", "code_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    app_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    module_name: Mapped[str] = mapped_column(String(255), nullable=False)
    debug_file: Mapped[str] = mapped_column(String(255), nullable=False)
    debug_id: Mapped[str] = mapped_column(String(128), nullable=False)
    code_file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    code_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    has_pe_unwind: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sym_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    sym_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    raw_archive_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    raw_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_used_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        nullable=False,
        default=utcnow,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="symbols")


class Issue(Base):
    __tablename__ = "issues"
    __table_args__ = (
        Index("ix_issues_project_status", "project_id", "status"),
        Index(
            "ix_issues_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        Index(
            "ix_issues_root_cause_trgm",
            "root_cause",
            postgresql_using="gin",
            postgresql_ops={"root_cause": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="NEW")
    component: Mapped[str | None] = mapped_column(String(128), nullable=True)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_patterns_json: Mapped[list[str]] = mapped_column(JSONBType, nullable=False, default=list)
    fixed_in_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_ambiguous_stack: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    crash_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="issues")
    clusters: Mapped[list[CrashCluster]] = relationship(back_populates="issue")
    crash_reports: Mapped[list[CrashReport]] = relationship(back_populates="issue")


class CrashCluster(Base):
    __tablename__ = "crash_clusters"
    __table_args__ = (
        Index(
            "ix_crash_clusters_signature_text_trgm",
            "signature_text",
            postgresql_using="gin",
            postgresql_ops={"signature_text": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    issue_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("issues.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    signature_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    signature_frames_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONBType,
        nullable=False,
        default=list,
    )
    signature_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    exception_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    crash_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    project: Mapped[Project] = relationship(back_populates="clusters")
    issue: Mapped[Issue | None] = relationship(back_populates="clusters")
    crash_reports: Mapped[list[CrashReport]] = relationship(back_populates="cluster")


class CrashReport(Base):
    __tablename__ = "crash_reports"
    __table_args__ = (
        Index("ix_crash_reports_project_status_created", "project_id", "status", "created_at"),
        Index("ix_crash_reports_is_pinned_created", "is_pinned", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("crash_clusters.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    issue_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("issues.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    app_version: Mapped[str] = mapped_column(String(128), nullable=False)
    build_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    os_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dump_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    dump_raw_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    dump_compressed_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    crash_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    crash_address: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parsed_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSONBType, nullable=True)
    parsed_stacks_json: Mapped[dict[str, Any] | None] = mapped_column(JSONBType, nullable=True)
    missing_symbols_json: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONBType,
        nullable=True,
    )
    ai_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_human_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    crash_time: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="crash_reports")
    cluster: Mapped[CrashCluster | None] = relationship(back_populates="crash_reports")
    issue: Mapped[Issue | None] = relationship(back_populates="crash_reports")
    logs: Mapped[list[CrashLog]] = relationship(
        back_populates="crash_report",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    ai_runs: Mapped[list[AIAnalysisRun]] = relationship(
        back_populates="crash_report",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AIAnalysisRun.created_at.desc()",
    )


class CrashJob(Base):
    """A recoverable unit of background work, with one active job per report."""

    __tablename__ = "crash_jobs"
    __table_args__ = (Index("ix_crash_jobs_ready", "lease_expires_at", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True)
    crash_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("crash_reports.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    task: Mapped[str] = mapped_column(String(32), nullable=False)
    run_ai_override: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    lease_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)


class CrashLog(Base):
    __tablename__ = "crash_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    crash_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("crash_reports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    zst_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    idx_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    raw_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    compressed_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_lines: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    digest_json: Mapped[dict[str, Any]] = mapped_column(JSONBType, nullable=False, default=dict)

    crash_report: Mapped[CrashReport] = relationship(back_populates="logs")


class AIAnalysisRun(Base):
    __tablename__ = "ai_analysis_runs"
    __table_args__ = (
        Index("ix_ai_analysis_runs_crash_id_created", "crash_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    crash_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("crash_reports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RUNNING")
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONBType, nullable=True)
    trace_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONBType,
        nullable=False,
        default=list,
    )
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)

    crash_report: Mapped[CrashReport] = relationship(back_populates="ai_runs")


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSONBType, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )
