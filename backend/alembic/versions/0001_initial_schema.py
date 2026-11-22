"""Initial CrashKiller schema with pg_trgm extension and GIN trigram indexes.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-22 11:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb_type() -> sa.JSON[sa.Any]:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")

    # 1. projects
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("api_key_hash", sa.String(length=128), nullable=False),
        sa.Column("settings_json", _jsonb_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
        sa.UniqueConstraint("key", name="uq_projects_key"),
    )
    op.create_index("ix_projects_key", "projects", ["key"], unique=True)

    # 2. issues
    op.create_table(
        "issues",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("component", sa.String(length=128), nullable=True),
        sa.Column("root_cause", sa.Text(), nullable=True),
        sa.Column("log_patterns_json", _jsonb_type(), nullable=False),
        sa.Column("fixed_in_version", sa.String(length=128), nullable=True),
        sa.Column("is_ambiguous_stack", sa.Boolean(), nullable=False),
        sa.Column("is_pinned", sa.Boolean(), nullable=False),
        sa.Column("crash_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_issues_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_issues"),
    )
    op.create_index("ix_issues_project_id", "issues", ["project_id"])
    op.create_index("ix_issues_project_status", "issues", ["project_id", "status"])
    op.create_index("ix_issues_is_pinned", "issues", ["is_pinned"])

    # 3. crash_clusters
    op.create_table(
        "crash_clusters",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("project_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("issue_id", sa.Integer(), nullable=True),
        sa.Column("signature_hash", sa.String(length=128), nullable=False),
        sa.Column("signature_frames_json", _jsonb_type(), nullable=False),
        sa.Column("signature_text", sa.Text(), nullable=False),
        sa.Column("exception_type", sa.String(length=255), nullable=True),
        sa.Column("crash_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["issue_id"],
            ["issues.id"],
            name="fk_crash_clusters_issue_id_issues",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_crash_clusters_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_crash_clusters"),
        sa.UniqueConstraint("signature_hash", name="uq_crash_clusters_signature_hash"),
    )
    op.create_index("ix_crash_clusters_project_id", "crash_clusters", ["project_id"])
    op.create_index("ix_crash_clusters_issue_id", "crash_clusters", ["issue_id"])

    # 4. symbols
    op.create_table(
        "symbols",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("project_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("app_version", sa.String(length=128), nullable=False),
        sa.Column("module_name", sa.String(length=255), nullable=False),
        sa.Column("debug_file", sa.String(length=255), nullable=False),
        sa.Column("debug_id", sa.String(length=128), nullable=False),
        sa.Column("code_file", sa.String(length=255), nullable=True),
        sa.Column("code_id", sa.String(length=128), nullable=True),
        sa.Column("has_pe_unwind", sa.Boolean(), nullable=False),
        sa.Column("sym_path", sa.String(length=1024), nullable=False),
        sa.Column("sym_size", sa.BigInteger(), nullable=False),
        sa.Column("raw_archive_path", sa.String(length=1024), nullable=True),
        sa.Column("raw_size", sa.BigInteger(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_symbols_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_symbols"),
        sa.UniqueConstraint(
            "project_id",
            "debug_file",
            "debug_id",
            name="uq_symbols_project_debug_file_id",
        ),
    )
    op.create_index("ix_symbols_project_id", "symbols", ["project_id"])
    op.create_index("ix_symbols_app_version", "symbols", ["app_version"])
    op.create_index("ix_symbols_debug_file_debug_id", "symbols", ["debug_file", "debug_id"])
    op.create_index("ix_symbols_code_file_code_id", "symbols", ["code_file", "code_id"])
    op.create_index("ix_symbols_last_used_at", "symbols", ["last_used_at"])

    # 5. crash_reports
    op.create_table(
        "crash_reports",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("project_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("cluster_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("issue_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("app_version", sa.String(length=128), nullable=False),
        sa.Column("build_id", sa.String(length=128), nullable=True),
        sa.Column("os_version", sa.String(length=255), nullable=True),
        sa.Column("dump_path", sa.String(length=1024), nullable=True),
        sa.Column("dump_raw_size", sa.BigInteger(), nullable=False),
        sa.Column("dump_compressed_size", sa.BigInteger(), nullable=False),
        sa.Column("crash_reason", sa.String(length=255), nullable=True),
        sa.Column("crash_address", sa.String(length=128), nullable=True),
        sa.Column("parsed_summary_json", _jsonb_type(), nullable=True),
        sa.Column("parsed_stacks_json", _jsonb_type(), nullable=True),
        sa.Column("missing_symbols_json", _jsonb_type(), nullable=True),
        sa.Column("ai_verdict", sa.String(length=32), nullable=True),
        sa.Column("ai_confidence", sa.Float(), nullable=True),
        sa.Column("is_human_verified", sa.Boolean(), nullable=False),
        sa.Column("is_pinned", sa.Boolean(), nullable=False),
        sa.Column("crash_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["cluster_id"],
            ["crash_clusters.id"],
            name="fk_crash_reports_cluster_id_crash_clusters",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["issue_id"],
            ["issues.id"],
            name="fk_crash_reports_issue_id_issues",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_crash_reports_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_crash_reports"),
    )
    op.create_index("ix_crash_reports_project_id", "crash_reports", ["project_id"])
    op.create_index("ix_crash_reports_cluster_id", "crash_reports", ["cluster_id"])
    op.create_index("ix_crash_reports_issue_id", "crash_reports", ["issue_id"])
    op.create_index("ix_crash_reports_ai_verdict", "crash_reports", ["ai_verdict"])
    op.create_index(
        "ix_crash_reports_project_status_created",
        "crash_reports",
        ["project_id", "status", "created_at"],
    )
    op.create_index(
        "ix_crash_reports_is_pinned_created",
        "crash_reports",
        ["is_pinned", "created_at"],
    )

    # 6. crash_logs
    op.create_table(
        "crash_logs",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("crash_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("zst_path", sa.String(length=1024), nullable=True),
        sa.Column("idx_path", sa.String(length=1024), nullable=True),
        sa.Column("raw_size", sa.BigInteger(), nullable=False),
        sa.Column("compressed_size", sa.BigInteger(), nullable=False),
        sa.Column("total_lines", sa.Integer(), nullable=False),
        sa.Column("digest_json", _jsonb_type(), nullable=False),
        sa.ForeignKeyConstraint(
            ["crash_id"],
            ["crash_reports.id"],
            name="fk_crash_logs_crash_id_crash_reports",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_crash_logs"),
    )
    op.create_index("ix_crash_logs_crash_id", "crash_logs", ["crash_id"])

    # 7. ai_analysis_runs
    op.create_table(
        "ai_analysis_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("crash_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("model_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_json", _jsonb_type(), nullable=True),
        sa.Column("trace_json", _jsonb_type(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["crash_id"],
            ["crash_reports.id"],
            name="fk_ai_analysis_runs_crash_id_crash_reports",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_analysis_runs"),
    )
    op.create_index("ix_ai_analysis_runs_crash_id", "ai_analysis_runs", ["crash_id"])
    op.create_index(
        "ix_ai_analysis_runs_crash_id_created",
        "ai_analysis_runs",
        ["crash_id", "created_at"],
    )

    # 8. system_settings
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value_json", _jsonb_type(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name="pk_system_settings"),
    )

    # PostgreSQL pg_trgm GIN indexes
    if is_pg:
        op.create_index(
            "ix_issues_title_trgm",
            "issues",
            ["title"],
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        )
        op.create_index(
            "ix_issues_root_cause_trgm",
            "issues",
            ["root_cause"],
            postgresql_using="gin",
            postgresql_ops={"root_cause": "gin_trgm_ops"},
        )
        op.create_index(
            "ix_crash_clusters_signature_text_trgm",
            "crash_clusters",
            ["signature_text"],
            postgresql_using="gin",
            postgresql_ops={"signature_text": "gin_trgm_ops"},
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.drop_index("ix_crash_clusters_signature_text_trgm", table_name="crash_clusters")
        op.drop_index("ix_issues_root_cause_trgm", table_name="issues")
        op.drop_index("ix_issues_title_trgm", table_name="issues")

    op.drop_table("system_settings")
    op.drop_table("ai_analysis_runs")
    op.drop_table("crash_logs")
    op.drop_table("crash_reports")
    op.drop_table("symbols")
    op.drop_table("crash_clusters")
    op.drop_table("issues")
    op.drop_table("projects")
