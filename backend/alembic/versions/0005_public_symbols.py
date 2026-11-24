"""Support project-less, version-less public symbols in the symbols table."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005_public_symbols"
down_revision = "0004_crash_session_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("symbols") as batch_op:
        batch_op.alter_column(
            "project_id",
            existing_type=sa.Uuid(as_uuid=True),
            nullable=True,
        )
        batch_op.alter_column(
            "app_version",
            existing_type=sa.String(length=128),
            nullable=True,
        )
        batch_op.add_column(
            sa.Column(
                "source",
                sa.String(length=32),
                nullable=False,
                server_default="upload",
            )
        )

    op.create_index("ix_symbols_source", "symbols", ["source"], unique=False)
    op.create_index(
        "uq_symbols_public_debug_file_id",
        "symbols",
        ["debug_file", "debug_id"],
        unique=True,
        sqlite_where=sa.text("project_id IS NULL"),
        postgresql_where=sa.text("project_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_symbols_public_debug_file_id", table_name="symbols")
    op.drop_index("ix_symbols_source", table_name="symbols")
    op.execute(sa.text("DELETE FROM symbols WHERE project_id IS NULL"))
    with op.batch_alter_table("symbols") as batch_op:
        batch_op.drop_column("source")
        batch_op.alter_column(
            "app_version",
            existing_type=sa.String(length=128),
            nullable=False,
        )
        batch_op.alter_column(
            "project_id",
            existing_type=sa.Uuid(as_uuid=True),
            nullable=False,
        )
