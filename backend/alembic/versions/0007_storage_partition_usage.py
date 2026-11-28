"""Add storage_partition_usage ledger table for O(1) quota accounting."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007_storage_partition_usage"
down_revision = "0006_issue_status_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "storage_partition_usage",
        sa.Column("partition", sa.String(length=64), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("partition", name="pk_storage_partition_usage"),
    )


def downgrade() -> None:
    op.drop_table("storage_partition_usage")
