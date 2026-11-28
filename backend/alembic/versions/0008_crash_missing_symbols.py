"""Add crash_missing_symbols index table, GC partial indexes, and log staging column."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from alembic import context, op

revision = "0008_crash_missing_symbols"
down_revision = "0007_storage_partition_usage"
branch_labels = None
depends_on = None

_ALL_ZERO_DEBUG_ID_RE = re.compile(r"^0+$")
_ABS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _is_safe_segment(value: str) -> bool:
    if not value or value in {".", ".."} or "\x00" in value:
        return False
    if "/" in value or "\\" in value or ".." in value or ":" in value:
        return False
    return True


def _normalize_debug_file(raw: str) -> str:
    """Match live ms_symbol_service.normalize_debug_file_name validation rules."""
    cleaned = raw.strip()
    if not cleaned or "\x00" in cleaned:
        return cleaned
    if "/" not in cleaned and "\\" not in cleaned:
        return cleaned
    is_abs = bool(_ABS_DRIVE_PATH_RE.match(cleaned)) or cleaned.startswith("/")
    if not is_abs:
        return cleaned
    parts = [p for p in cleaned.replace("\\", "/").split("/") if p]
    if any(p == ".." or ".." in p for p in parts):
        return cleaned
    leaf = parts[-1] if parts else ""
    return leaf if _is_safe_segment(leaf) else cleaned


def _normalize_entries_for_migration(raw_json: Any) -> list[dict[str, Any]]:
    if isinstance(raw_json, str):
        try:
            raw_json = json.loads(raw_json)
        except ValueError:
            return []
    if not isinstance(raw_json, list) or not raw_json:
        return []
    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []
    for entry in raw_json:
        if not isinstance(entry, dict):
            continue
        raw_df = entry.get("debug_file")
        raw_did = entry.get("debug_id")
        debug_file = _normalize_debug_file(str(raw_df)) if raw_df is not None else ""
        debug_id = str(raw_did).strip().upper() if raw_did is not None else ""
        if not debug_file and (not debug_id or _ALL_ZERO_DEBUG_ID_RE.fullmatch(debug_id)):
            continue
        debug_file = debug_file[:255]
        debug_file_lower = debug_file.lower()[:255]
        debug_id = debug_id[:128]
        key = (debug_file_lower, debug_id)
        if key in seen:
            continue
        seen.add(key)
        raw_mod = entry.get("module_name")
        module_name = (
            str(raw_mod).strip()[:255]
            if raw_mod is not None and str(raw_mod).strip()
            else (debug_file or "unknown")
        )
        code_file = (
            str(entry["code_file"]).strip()[:255]
            if entry.get("code_file") is not None and str(entry["code_file"]).strip()
            else None
        )
        code_id = (
            str(entry["code_id"]).strip().upper()[:128]
            if entry.get("code_id") is not None and str(entry["code_id"]).strip()
            else None
        )
        rows.append(
            {
                "debug_file": debug_file,
                "debug_file_lower": debug_file_lower,
                "debug_id": debug_id,
                "module_name": module_name,
                "code_file": code_file,
                "code_id": code_id,
            }
        )
    return rows


def _as_utc(dt: Any) -> datetime:
    if isinstance(dt, datetime):
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return datetime.fromtimestamp(0, tz=UTC)


def _backfill_existing_missing_symbols(bind: sa.Connection) -> None:
    crash_reports = sa.table(
        "crash_reports",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("project_id", sa.Uuid(as_uuid=True)),
        sa.column("missing_symbols_json", sa.JSON()),
        sa.column("missing_symbols_indexed", sa.Boolean()),
        sa.column("crash_time", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    missing_tbl = sa.table(
        "crash_missing_symbols",
        sa.column("crash_id", sa.Uuid(as_uuid=True)),
        sa.column("debug_file_lower", sa.String(length=255)),
        sa.column("debug_id", sa.String(length=128)),
        sa.column("project_id", sa.Uuid(as_uuid=True)),
        sa.column("debug_file", sa.String(length=255)),
        sa.column("module_name", sa.String(length=255)),
        sa.column("code_file", sa.String(length=255)),
        sa.column("code_id", sa.String(length=128)),
        sa.column("crash_time", sa.DateTime(timezone=True)),
    )

    batch_size = 500
    last_id: Any = None
    while True:
        stmt = (
            sa.select(
                crash_reports.c.id,
                crash_reports.c.project_id,
                crash_reports.c.missing_symbols_json,
                crash_reports.c.crash_time,
                crash_reports.c.created_at,
            )
            .where(
                crash_reports.c.missing_symbols_json.is_not(None),
                crash_reports.c.missing_symbols_indexed.is_(False),
            )
            .order_by(crash_reports.c.id.asc())
            .limit(batch_size)
        )
        if last_id is not None:
            stmt = stmt.where(crash_reports.c.id > last_id)

        rows = bind.execute(stmt).fetchall()
        if not rows:
            break
        last_id = rows[-1][0]

        batch_ids: list[Any] = []
        inserts: list[dict[str, Any]] = []
        for cid, pid, raw_json, crash_time, created_at in rows:
            batch_ids.append(cid)
            event_ts = _as_utc(crash_time or created_at)
            for item in _normalize_entries_for_migration(raw_json):
                inserts.append(
                    {
                        "crash_id": cid,
                        "project_id": pid,
                        "debug_file": item["debug_file"],
                        "debug_file_lower": item["debug_file_lower"],
                        "debug_id": item["debug_id"],
                        "module_name": item["module_name"],
                        "code_file": item["code_file"],
                        "code_id": item["code_id"],
                        "crash_time": event_ts,
                    }
                )
        if inserts:
            bind.execute(sa.insert(missing_tbl), inserts)
        bind.execute(
            sa.update(crash_reports)
            .where(crash_reports.c.id.in_(batch_ids))
            .values(missing_symbols_indexed=True)
        )
        if len(rows) < batch_size:
            break


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    op.add_column(
        "crash_reports",
        sa.Column(
            "missing_symbols_indexed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "crash_logs",
        sa.Column("staged_raw_path", sa.String(length=1024), nullable=True),
    )

    op.create_table(
        "crash_missing_symbols",
        sa.Column("crash_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("debug_file_lower", sa.String(length=255), nullable=False),
        sa.Column("debug_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("debug_file", sa.String(length=255), nullable=False),
        sa.Column("module_name", sa.String(length=255), nullable=False, server_default="unknown"),
        sa.Column("code_file", sa.String(length=255), nullable=True),
        sa.Column("code_id", sa.String(length=128), nullable=True),
        sa.Column("crash_time", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["crash_id"],
            ["crash_reports.id"],
            name="fk_crash_missing_symbols_crash_id_crash_reports",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_crash_missing_symbols_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "crash_id",
            "debug_file_lower",
            "debug_id",
            name="pk_crash_missing_symbols",
        ),
    )
    op.create_index(
        "ix_crash_missing_symbols_project_id",
        "crash_missing_symbols",
        ["project_id"],
    )
    op.create_index(
        "ix_crash_missing_symbols_project_debug_id",
        "crash_missing_symbols",
        ["project_id", "debug_id"],
    )
    op.create_index(
        "ix_crash_missing_symbols_key",
        "crash_missing_symbols",
        ["debug_file_lower", "debug_id"],
    )

    if is_pg:
        op.create_index(
            "ix_crash_reports_dump_created",
            "crash_reports",
            ["created_at"],
            postgresql_where=sa.text("dump_path IS NOT NULL"),
        )
        op.create_index(
            "ix_crash_reports_missing_unindexed",
            "crash_reports",
            ["id"],
            postgresql_where=sa.text(
                "missing_symbols_json IS NOT NULL AND missing_symbols_indexed = false"
            ),
        )
        op.create_index(
            "ix_crash_logs_zst_crash",
            "crash_logs",
            ["crash_id"],
            postgresql_where=sa.text("zst_path IS NOT NULL"),
        )
    else:
        op.create_index(
            "ix_crash_reports_dump_created",
            "crash_reports",
            ["created_at"],
            sqlite_where=sa.text("dump_path IS NOT NULL"),
        )
        op.create_index(
            "ix_crash_reports_missing_unindexed",
            "crash_reports",
            ["id"],
            sqlite_where=sa.text(
                "missing_symbols_json IS NOT NULL AND missing_symbols_indexed = 0"
            ),
        )
        op.create_index(
            "ix_crash_logs_zst_crash",
            "crash_logs",
            ["crash_id"],
            sqlite_where=sa.text("zst_path IS NOT NULL"),
        )

    if not context.is_offline_mode():
        _backfill_existing_missing_symbols(bind)


def downgrade() -> None:
    op.drop_index("ix_crash_logs_zst_crash", table_name="crash_logs")
    op.drop_index("ix_crash_reports_missing_unindexed", table_name="crash_reports")
    op.drop_index("ix_crash_reports_dump_created", table_name="crash_reports")
    op.drop_index("ix_crash_missing_symbols_key", table_name="crash_missing_symbols")
    op.drop_index("ix_crash_missing_symbols_project_debug_id", table_name="crash_missing_symbols")
    op.drop_index("ix_crash_missing_symbols_project_id", table_name="crash_missing_symbols")
    op.drop_table("crash_missing_symbols")
    op.drop_column("crash_logs", "staged_raw_path")
    op.drop_column("crash_reports", "missing_symbols_indexed")
