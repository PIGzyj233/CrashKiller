"""Add issue_status_events table for tracking issue lifecycle transitions."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006_issue_status_events"
down_revision = "0005_public_symbols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "issue_status_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("issue_id", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default="manual",
        ),
        sa.Column("fixed_in_version", sa.String(length=128), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("crash_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["issue_id"],
            ["issues.id"],
            name="fk_issue_status_events_issue_id_issues",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["crash_id"],
            ["crash_reports.id"],
            name="fk_issue_status_events_crash_id_crash_reports",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_issue_status_events"),
    )
    op.create_index(
        "ix_issue_status_events_issue_id",
        "issue_status_events",
        ["issue_id"],
        unique=False,
    )
    op.create_index(
        "ix_issue_status_events_issue_created",
        "issue_status_events",
        ["issue_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_issue_status_events_issue_created", table_name="issue_status_events")
    op.drop_index("ix_issue_status_events_issue_id", table_name="issue_status_events")
    op.drop_table("issue_status_events")
