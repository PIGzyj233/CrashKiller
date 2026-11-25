"""FastAPI application factory, ASGI lifespan, health check, and router mounts."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from arq.connections import ArqRedis
from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import require_admin, require_symsrv_access
from app.api.v1.router import api_v1_router
from app.api.v1.symbols import symsrv_router
from app.config import Settings, get_settings
from app.db.base import Base
from app.db.session import dispose_engine, init_db
from app.services.events import (
    EventBus,
    close_event_bus,
    create_event_bus,
    set_event_bus,
)
from app.services.storage import StorageManager
from app.workers.worker import TaskDispatcher


def create_app(
    settings: Settings | None = None,
    *,
    disk_usage_fn: Callable[[Path | str], Any] | None = None,
    exec_fn: Any = None,
    openai_client: Any = None,
    event_bus: EventBus | None = None,
    arq_pool: ArqRedis | None = None,
) -> FastAPI:
    """Create and configure the CrashKiller FastAPI application."""
    cfg_init = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        cfg = settings or get_settings()
        engine = init_db(cfg.DATABASE_URL, echo=cfg.DEBUG)
        if "sqlite" in cfg.DATABASE_URL.lower():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        storage = StorageManager(
            data_dir=cfg.DATA_DIR,
            settings=cfg,
            disk_usage_fn=disk_usage_fn,
        )
        bus = event_bus if event_bus is not None else create_event_bus(cfg.REDIS_URL)
        set_event_bus(bus, cfg.REDIS_URL)

        dispatcher = TaskDispatcher(
            settings=cfg,
            storage=storage,
            event_bus=bus,
            session_factory=session_factory,
            arq_pool=arq_pool,
            exec_fn=exec_fn,
            openai_client=openai_client,
        )

        application.state.settings = cfg
        application.state.engine = engine
        application.state.session_factory = session_factory
        application.state.storage = storage
        application.state.event_bus = bus
        application.state.dispatcher = dispatcher
        application.state.exec_fn = exec_fn
        application.state.openai_client = openai_client

        try:
            dispatcher.start_memory_recovery()
            yield
        finally:
            await dispatcher.drain_and_cancel(timeout=5.0)
            await close_event_bus()
            await dispose_engine()

    application = FastAPI(
        title=cfg_init.APP_NAME,
        version=cfg_init.APP_VERSION,
        debug=cfg_init.DEBUG,
        lifespan=lifespan,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=[origin for origin in cfg_init.CORS_ORIGINS if origin != "*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Total-Count"],
    )

    def _sanitize_json_errors(obj: Any) -> Any:
        if isinstance(obj, float) and not math.isfinite(obj):
            return str(obj)
        if isinstance(obj, dict):
            return {k: _sanitize_json_errors(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_sanitize_json_errors(item) for item in obj]
        return obj

    @application.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        safe_detail = _sanitize_json_errors(jsonable_encoder(exc.errors()))
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": safe_detail},
        )

    @application.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get(
        "/internal/admin-auth",
        dependencies=[Depends(require_admin)],
        include_in_schema=False,
    )
    async def admin_auth() -> Response:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    application.include_router(symsrv_router, dependencies=[Depends(require_symsrv_access)])
    application.include_router(api_v1_router, prefix=cfg_init.API_V1_PREFIX)

    return application


app = create_app()

__all__ = ["app", "create_app"]
