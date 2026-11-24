"""Add optional custom session_id column and index on crash_reports."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_crash_session_id"
down_revision = "0003_project_api_key_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("crash_reports", sa.Column("session_id", sa.String(length=128), nullable=True))
    op.create_index(
        "ix_crash_reports_project_session_created",
        "crash_reports",
        ["project_id", "session_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_crash_reports_project_session_created", table_name="crash_reports")
    op.drop_column("crash_reports", "session_id")
