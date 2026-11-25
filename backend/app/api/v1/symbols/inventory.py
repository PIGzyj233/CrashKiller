"""Symbol inventory listing and reference-counted deletion endpoints."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_storage
from app.api.v1.symbols.helpers import _unlink_and_prune
from app.db.models import Symbol
from app.schemas.symbol import SymbolListResponse, SymbolRead
from app.services import symbol_service
from app.services.storage import StorageManager

inventory_router = APIRouter()


async def list_symbols(
    project_id: uuid.UUID | None = None,
    scope: Literal["project", "public", "all"] | None = Query(default=None),
    source: str | None = Query(default=None),
    app_version: str | None = None,
    q: str | None = None,
    has_pe_unwind: bool | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
) -> SymbolListResponse:
    if scope in {"public", "all"}:
        await symbol_service.reconcile_public_symbol_cache(session, storage)

    filters = []
    if scope == "public":
        filters.append(Symbol.project_id.is_(None))
    elif scope == "all":
        if project_id is not None:
            filters.append(or_(Symbol.project_id == project_id, Symbol.project_id.is_(None)))
    else:
        filters.append(Symbol.project_id.is_not(None))
        if project_id is not None:
            filters.append(Symbol.project_id == project_id)

    if source is not None and source.strip():
        filters.append(Symbol.source == source.strip())
    if scope != "public" and app_version is not None and app_version.strip():
        filters.append(Symbol.app_version == app_version.strip())
    if has_pe_unwind is not None:
        filters.append(Symbol.has_pe_unwind == has_pe_unwind)
    if q is not None and q.strip():
        pattern = f"%{q.strip()}%"
        filters.append(
            or_(
                Symbol.debug_file.ilike(pattern),
                Symbol.module_name.ilike(pattern),
                Symbol.code_file.ilike(pattern),
                Symbol.debug_id.ilike(pattern),
            )
        )

    count_stmt = select(func.count()).select_from(Symbol)
    list_stmt = select(Symbol)
    if filters:
        count_stmt = count_stmt.where(*filters)
        list_stmt = list_stmt.where(*filters)

    total = int((await session.execute(count_stmt)).scalar_one())
    rows = (
        await session.execute(
            list_stmt.order_by(Symbol.created_at.desc()).offset(offset).limit(limit)
        )
    ).scalars().all()
    return SymbolListResponse(
        items=[SymbolRead.model_validate(row) for row in rows],
        total=total,
    )


@inventory_router.delete("/{symbol_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_symbol(
    symbol_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
) -> Response:
    sym = await session.get(Symbol, symbol_id)
    if sym is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Symbol not found")

    sym_path = sym.sym_path
    sym_partition = (
        "symbols/public_cache"
        if sym.project_id is None or (sym_path or "").startswith("symbols/public_cache/")
        else "symbols/sym"
    )
    raw_archive_path = sym.raw_archive_path
    code_id = sym.code_id
    code_file = sym.code_file

    await session.delete(sym)
    await session.flush()

    if sym_path:
        sym_ref_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(Symbol).where(Symbol.sym_path == sym_path)
                )
            ).scalar_one()
        )
        if sym_ref_count == 0:
            _unlink_and_prune(storage, sym_path, sym_partition)

    if raw_archive_path:
        raw_ref_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Symbol)
                    .where(Symbol.raw_archive_path == raw_archive_path)
                )
            ).scalar_one()
        )
        if raw_ref_count == 0:
            _unlink_and_prune(storage, raw_archive_path, "symbols/raw")

    if code_id and code_file:
        pe_raw_rel = f"symbols/raw/{code_id}/{code_file}.zst"
        pe_ref_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Symbol)
                    .where(
                        or_(
                            and_(
                                Symbol.code_id == code_id,
                                Symbol.code_file == code_file,
                                Symbol.has_pe_unwind.is_(True),
                            ),
                            Symbol.raw_archive_path == pe_raw_rel,
                        )
                    )
                )
            ).scalar_one()
        )
        if pe_ref_count == 0:
            _unlink_and_prune(storage, pe_raw_rel, "symbols/raw")

    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
