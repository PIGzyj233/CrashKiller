"""Indexed missing-symbol persistence, locked backfill, and read-only SQL aggregation."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CrashMissingSymbol, CrashReport
from app.services import ms_symbol_service

_ALL_ZERO_DEBUG_ID_RE = re.compile(r"^0+$")


def _as_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.fromtimestamp(0, tz=UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _is_postgresql(session: AsyncSession) -> bool:
    bind = session.get_bind()
    return bind is not None and bind.dialect.name == "postgresql"


def normalize_missing_entries(
    missing_symbols_json: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Normalize and deduplicate raw missing_symbols_json entries for a single crash."""
    if not isinstance(missing_symbols_json, list) or not missing_symbols_json:
        return []

    seen: set[tuple[str, str]] = set()
    normalized: list[dict[str, Any]] = []
    for entry in missing_symbols_json:
        if not isinstance(entry, dict):
            continue
        raw_df = entry.get("debug_file")
        raw_did = entry.get("debug_id")
        raw_df_str = str(raw_df).strip() if raw_df is not None else ""
        debug_file = (
            ms_symbol_service.normalize_debug_file_name(raw_df_str) if raw_df_str else ""
        )
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
        normalized.append(
            {
                "debug_file": debug_file,
                "debug_file_lower": debug_file_lower,
                "debug_id": debug_id,
                "module_name": module_name,
                "code_file": code_file,
                "code_id": code_id,
            }
        )
    return normalized


async def sync_crash_missing_symbols(
    session: AsyncSession,
    crash: CrashReport,
) -> None:
    """Replace indexed CrashMissingSymbol rows for crash and mark it indexed."""
    await session.execute(
        delete(CrashMissingSymbol).where(CrashMissingSymbol.crash_id == crash.id)
    )
    entries = normalize_missing_entries(crash.missing_symbols_json)
    event_ts = _as_utc(crash.crash_time or crash.created_at)
    for item in entries:
        session.add(
            CrashMissingSymbol(
                crash_id=crash.id,
                project_id=crash.project_id,
                debug_file=item["debug_file"],
                debug_file_lower=item["debug_file_lower"],
                debug_id=item["debug_id"],
                module_name=item["module_name"],
                code_file=item["code_file"],
                code_id=item["code_id"],
                crash_time=event_ts,
            )
        )
    crash.missing_symbols_indexed = True
    await session.flush()


async def backfill_unindexed_missing_symbols(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    batch_size: int = 500,
    max_batches: int | None = None,
    commit_per_batch: bool = False,
) -> int:
    """Lock and sync legacy CrashReports with missing_symbols_indexed=False in ordered batches.

    Uses keyset pagination and ``FOR UPDATE SKIP LOCKED`` (PostgreSQL) with
    ``populate_existing=True`` so stale pre-loaded JSON can never overwrite a fresh worker index.
    """
    is_pg = _is_postgresql(session)
    total_synced = 0
    batches = 0
    last_id: uuid.UUID | None = None

    while True:
        id_stmt = (
            select(CrashReport.id)
            .where(
                CrashReport.missing_symbols_json.is_not(None),
                CrashReport.missing_symbols_indexed.is_(False),
            )
            .order_by(CrashReport.id.asc())
            .limit(batch_size)
        )
        if project_id is not None:
            id_stmt = id_stmt.where(CrashReport.project_id == project_id)
        if last_id is not None:
            id_stmt = id_stmt.where(CrashReport.id > last_id)

        candidate_ids = list((await session.scalars(id_stmt)).all())
        if not candidate_ids:
            break
        last_id = candidate_ids[-1]

        lock_stmt = (
            select(CrashReport)
            .where(
                CrashReport.id.in_(candidate_ids),
                CrashReport.missing_symbols_json.is_not(None),
                CrashReport.missing_symbols_indexed.is_(False),
            )
            .order_by(CrashReport.id.asc())
            .execution_options(populate_existing=True)
        )
        if is_pg:
            lock_stmt = lock_stmt.with_for_update(skip_locked=True)

        locked_rows = list((await session.execute(lock_stmt)).scalars().all())
        for crash in locked_rows:
            await sync_crash_missing_symbols(session, crash)
            total_synced += 1
        if commit_per_batch and locked_rows:
            await session.commit()

        batches += 1
        if max_batches is not None and batches >= max_batches:
            break
        if len(candidate_ids) < batch_size:
            break

    return total_synced


def _build_bucket_from_detail_rows(
    debug_id: str,
    rows: list[tuple[uuid.UUID, uuid.UUID | None, str, str, str | None, str | None, datetime]],
) -> dict[str, Any]:
    """Build one missing-symbol bucket keeping (code_file, code_id) from one representative row.

    Rows must be ordered newest-first (crash_time DESC, crash_id ASC).
    """
    affected_crash_ids: list[uuid.UUID] = []
    seen_crashes: set[uuid.UUID] = set()
    project_ids: set[uuid.UUID] = set()
    latest_ts = datetime.fromtimestamp(0, tz=UTC)

    rep_debug_file = ""
    rep_module_name: str | None = None
    fallback_module_name: str | None = None
    best_code_pair: tuple[str | None, str | None] | None = None
    partial_code_pair: tuple[str | None, str | None] | None = None

    for cid, pid, debug_file, module_name, code_file, code_id, crash_time in rows:
        if cid not in seen_crashes:
            seen_crashes.add(cid)
            affected_crash_ids.append(cid)
        if pid is not None:
            project_ids.add(pid)
        ts = _as_utc(crash_time)
        if ts > latest_ts:
            latest_ts = ts
        if not rep_debug_file and debug_file:
            rep_debug_file = debug_file
        if module_name:
            if fallback_module_name is None:
                fallback_module_name = module_name
            if rep_module_name is None and module_name != "unknown":
                rep_module_name = module_name
        if best_code_pair is None and code_file and code_id:
            best_code_pair = (code_file, code_id)
        elif partial_code_pair is None and (code_file or code_id):
            partial_code_pair = (code_file, code_id)

    chosen_code_file, chosen_code_id = best_code_pair or partial_code_pair or (None, None)
    return {
        "debug_file": rep_debug_file,
        "debug_id": debug_id or "",
        "module_name": rep_module_name or fallback_module_name or "unknown",
        "code_file": chosen_code_file,
        "code_id": chosen_code_id,
        "latest_crash_at": latest_ts,
        "affected_crash_ids": affected_crash_ids,
        "project_ids": project_ids,
    }


async def _query_missing_with_unindexed_readonly(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None,
    unindexed_rows: list[Any],
    limit: int | None,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    """Read-only merge of indexed rows and any not-yet-backfilled CrashReport rows."""
    unindexed_crash_ids = {row[0] for row in unindexed_rows}
    grouped_details: dict[
        tuple[str, str],
        list[tuple[uuid.UUID, uuid.UUID | None, str, str, str | None, str | None, datetime]],
    ] = {}

    idx_stmt = select(
        CrashMissingSymbol.debug_file_lower,
        CrashMissingSymbol.debug_id,
        CrashMissingSymbol.crash_id,
        CrashMissingSymbol.project_id,
        CrashMissingSymbol.debug_file,
        CrashMissingSymbol.module_name,
        CrashMissingSymbol.code_file,
        CrashMissingSymbol.code_id,
        CrashMissingSymbol.crash_time,
    )
    if project_id is not None:
        idx_stmt = idx_stmt.where(CrashMissingSymbol.project_id == project_id)
    for (
        df_low,
        did,
        cid,
        pid,
        debug_file,
        module_name,
        code_file,
        code_id,
        crash_time,
    ) in (await session.execute(idx_stmt)).all():
        if cid in unindexed_crash_ids:
            continue
        grouped_details.setdefault((df_low, did), []).append(
            (cid, pid, debug_file or "", module_name or "unknown", code_file, code_id, crash_time)
        )

    for cid, pid, raw_json, crash_time, created_at in unindexed_rows:
        event_ts = _as_utc(crash_time or created_at)
        for item in normalize_missing_entries(raw_json):
            key = (item["debug_file_lower"], item["debug_id"])
            grouped_details.setdefault(key, []).append(
                (
                    cid,
                    pid,
                    item["debug_file"],
                    item["module_name"],
                    item["code_file"],
                    item["code_id"],
                    event_ts,
                )
            )

    if not grouped_details:
        return [], 0

    all_buckets: list[tuple[tuple[str, str], dict[str, Any]]] = []
    for key, rows in grouped_details.items():
        rows.sort(key=lambda r: (-_as_utc(r[6]).timestamp(), str(r[0])))
        all_buckets.append((key, _build_bucket_from_detail_rows(key[1], rows)))

    all_buckets.sort(
        key=lambda item: (
            -len(item[1]["affected_crash_ids"]),
            -item[1]["latest_crash_at"].timestamp(),
            item[0][0],
            item[0][1],
        )
    )
    total = len(all_buckets)
    sliced = (
        all_buckets[offset:]
        if limit is None
        else all_buckets[offset : offset + limit]
    )
    return [bucket for _, bucket in sliced], total


async def query_missing_symbols_aggregated(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    limit: int | None = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Read-only missing-symbol aggregation via indexed SQL GROUP BY over crash_missing_symbols."""
    unindexed_stmt = select(
        CrashReport.id,
        CrashReport.project_id,
        CrashReport.missing_symbols_json,
        CrashReport.crash_time,
        CrashReport.created_at,
    ).where(
        CrashReport.missing_symbols_json.is_not(None),
        CrashReport.missing_symbols_indexed.is_(False),
    )
    if project_id is not None:
        unindexed_stmt = unindexed_stmt.where(CrashReport.project_id == project_id)
    unindexed_rows = list((await session.execute(unindexed_stmt)).all())
    if unindexed_rows:
        return await _query_missing_with_unindexed_readonly(
            session,
            project_id=project_id,
            unindexed_rows=unindexed_rows,
            limit=limit,
            offset=offset,
        )

    base_filter = []
    if project_id is not None:
        base_filter.append(CrashMissingSymbol.project_id == project_id)

    distinct_subq = (
        select(
            CrashMissingSymbol.debug_file_lower,
            CrashMissingSymbol.debug_id,
        )
        .where(*base_filter)
        .group_by(CrashMissingSymbol.debug_file_lower, CrashMissingSymbol.debug_id)
        .subquery()
    )
    total = int(
        (await session.execute(select(func.count()).select_from(distinct_subq))).scalar_one()
    )
    if total == 0:
        return [], 0

    crash_count_col = func.count(CrashMissingSymbol.crash_id).label("crash_count")
    latest_ts_col = func.max(CrashMissingSymbol.crash_time).label("latest_crash_at")
    grouped_stmt = (
        select(
            CrashMissingSymbol.debug_file_lower,
            CrashMissingSymbol.debug_id,
            crash_count_col,
            latest_ts_col,
        )
        .where(*base_filter)
        .group_by(CrashMissingSymbol.debug_file_lower, CrashMissingSymbol.debug_id)
        .order_by(
            crash_count_col.desc(),
            latest_ts_col.desc(),
            CrashMissingSymbol.debug_file_lower.asc(),
            CrashMissingSymbol.debug_id.asc(),
        )
        .offset(offset)
    )
    if limit is not None:
        grouped_stmt = grouped_stmt.limit(limit)

    page_rows = (await session.execute(grouped_stmt)).all()
    if not page_rows:
        return [], total

    page_keys = [(row[0], row[1]) for row in page_rows]
    details_by_key: dict[
        tuple[str, str],
        list[tuple[uuid.UUID, uuid.UUID | None, str, str, str | None, str | None, datetime]],
    ] = {k: [] for k in page_keys}

    # Fetch detail rows only for the keys on this page, newest-first, so (code_file, code_id)
    # are always selected together from a single representative report.
    for start in range(0, len(page_keys), 200):
        chunk_keys = page_keys[start : start + 200]
        detail_stmt = (
            select(
                CrashMissingSymbol.debug_file_lower,
                CrashMissingSymbol.debug_id,
                CrashMissingSymbol.crash_id,
                CrashMissingSymbol.project_id,
                CrashMissingSymbol.debug_file,
                CrashMissingSymbol.module_name,
                CrashMissingSymbol.code_file,
                CrashMissingSymbol.code_id,
                CrashMissingSymbol.crash_time,
            )
            .where(
                *base_filter,
                tuple_(CrashMissingSymbol.debug_file_lower, CrashMissingSymbol.debug_id).in_(
                    chunk_keys
                ),
            )
            .order_by(
                CrashMissingSymbol.crash_time.desc(),
                CrashMissingSymbol.crash_id.asc(),
            )
        )
        for (
            df_low,
            did,
            cid,
            pid,
            debug_file,
            module_name,
            code_file,
            code_id,
            crash_time,
        ) in (await session.execute(detail_stmt)).all():
            details_by_key[(df_low, did)].append(
                (
                    cid,
                    pid,
                    debug_file or "",
                    module_name or "unknown",
                    code_file,
                    code_id,
                    crash_time,
                )
            )

    buckets: list[dict[str, Any]] = []
    for df_low, did, _cnt, _latest_ts in page_rows:
        buckets.append(_build_bucket_from_detail_rows(did, details_by_key[(df_low, did)]))
    return buckets, total


async def find_missing_symbol_crash_ids(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    debug_ids: set[str] | None = None,
    crash_ids: list[uuid.UUID] | None = None,
) -> set[uuid.UUID]:
    """Read-only lookup of crash IDs matching missing symbol filters."""
    norm_debug_ids: set[str] | None = None
    if debug_ids:
        norm = {d.strip().upper() for d in debug_ids if d and d.strip()}
        if not norm:
            return set()
        norm_debug_ids = norm

    if crash_ids is not None and not crash_ids:
        return set()

    stmt = select(CrashMissingSymbol.crash_id).distinct()
    if project_id is not None:
        stmt = stmt.where(CrashMissingSymbol.project_id == project_id)
    if norm_debug_ids is not None:
        stmt = stmt.where(CrashMissingSymbol.debug_id.in_(sorted(norm_debug_ids)))

    matched: set[uuid.UUID] = set()
    if crash_ids is not None:
        for start in range(0, len(crash_ids), 500):
            batch = crash_ids[start : start + 500]
            rows = (await session.scalars(stmt.where(CrashMissingSymbol.crash_id.in_(batch)))).all()
            matched.update(rows)
    else:
        matched.update((await session.scalars(stmt)).all())

    # Include any unindexed legacy crashes without mutating the caller's read session.
    unindexed_stmt = select(CrashReport.id, CrashReport.missing_symbols_json).where(
        CrashReport.missing_symbols_json.is_not(None),
        CrashReport.missing_symbols_indexed.is_(False),
    )
    if project_id is not None:
        unindexed_stmt = unindexed_stmt.where(CrashReport.project_id == project_id)
    if crash_ids is not None:
        unindexed_stmt = unindexed_stmt.where(CrashReport.id.in_(crash_ids))

    for cid, raw_json in (await session.execute(unindexed_stmt)).all():
        entries = normalize_missing_entries(raw_json)
        if not entries:
            continue
        if norm_debug_ids is None or any(item["debug_id"] in norm_debug_ids for item in entries):
            matched.add(cid)

    return matched


def build_missing_filter_clause(
    debug_ids: set[str] | None = None,
) -> Any:
    """Return an EXISTS clause against crash_missing_symbols for CrashReport queries."""
    conds = [CrashMissingSymbol.crash_id == CrashReport.id]
    if debug_ids:
        norm = sorted({d.strip().upper() for d in debug_ids if d and d.strip()})
        conds.append(CrashMissingSymbol.debug_id.in_(norm))
    return select(CrashMissingSymbol.crash_id).where(*conds).exists()


__all__ = [
    "backfill_unindexed_missing_symbols",
    "build_missing_filter_clause",
    "find_missing_symbol_crash_ids",
    "normalize_missing_entries",
    "or_",
    "query_missing_symbols_aggregated",
    "sync_crash_missing_symbols",
]
