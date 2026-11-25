"""WinDbg SymSrv (/symsrv) endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_storage
from app.services.storage import StorageManager
from app.services.symbol_service import resolve_symsrv_file, stream_decompressed_zst

symsrv_router = APIRouter(prefix="/symsrv", tags=["symsrv"])


@symsrv_router.get("/pingme.txt", response_class=PlainTextResponse)
async def symsrv_pingme() -> PlainTextResponse:
    return PlainTextResponse("", status_code=status.HTTP_200_OK)


@symsrv_router.get("/index2.txt")
async def symsrv_index2() -> Response:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


@symsrv_router.api_route(
    "/{filename}/{symbol_id}/{target_filename}",
    methods=["GET", "HEAD"],
)
async def symsrv_fetch_symbol(
    filename: str,
    symbol_id: str,
    target_filename: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
) -> Response:
    if target_filename.lower() != filename.lower():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Symbol not found")

    resolved = await resolve_symsrv_file(session, storage, filename, symbol_id)
    if resolved is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Symbol not found")

    zst_path, _ = resolved
    if not zst_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Symbol file missing")

    if request.method == "HEAD":
        return Response(
            content=b"",
            status_code=status.HTTP_200_OK,
            media_type="application/octet-stream",
        )

    return StreamingResponse(
        stream_decompressed_zst(zst_path),
        status_code=status.HTTP_200_OK,
        media_type="application/octet-stream",
    )
