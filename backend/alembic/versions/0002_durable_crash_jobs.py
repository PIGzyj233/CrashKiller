"""Persist outstanding crash processing independently of the Redis queue."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_durable_crash_jobs"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crash_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("crash_id", sa.Uuid(), nullable=False),
        sa.Column("task", sa.String(length=32), nullable=False),
        sa.Column("run_ai_override", sa.Boolean(), nullable=True),
        sa.Column("lease_id", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["crash_id"], ["crash_reports.id"],
            name="fk_crash_jobs_crash_id_crash_reports", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_crash_jobs"),
        sa.UniqueConstraint("crash_id", name="uq_crash_jobs_crash_id"),
    )
    op.create_index("ix_crash_jobs_ready", "crash_jobs", ["lease_expires_at", "created_at"])
    op.execute(
        """
        INSERT INTO crash_jobs (id, crash_id, task, run_ai_override, created_at)
        SELECT id, id,
               CASE WHEN status = 'TRIAGING' THEN 'ai_triage' ELSE 'parse_crash' END,
               CASE
                   WHEN status <> 'TRIAGING' AND CAST(parsed_stacks_json AS TEXT) <> 'null'
                   THEN TRUE
               END,
               CURRENT_TIMESTAMP
        FROM crash_reports
        WHERE status IN ('PENDING', 'PARSING', 'TRIAGING')
        """
    )


def downgrade() -> None:
    op.drop_table("crash_jobs")
