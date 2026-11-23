"""Persist retrievable project API keys for admin inspection."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_project_api_key_storage"
down_revision = "0002_durable_crash_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("api_key", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "api_key")
