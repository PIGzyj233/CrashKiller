"""ARQ worker settings, lifecycle hooks, and memory:// / Redis task dispatcher."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from arq import create_pool, cron, func
from arq.connections import ArqRedis, RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.session import dispose_engine, get_session_factory, init_db
from app.services.events import EventBus, close_event_bus, get_event_bus
from app.services.storage import StorageManager
from app.workers.jobs import (
    CRASH_JOB_TIMEOUT_SECONDS,
    JOB_DELIVERY_TIMEOUT_SECONDS,
    enqueue_crash_job,
    pending_crash_jobs,
)
from app.workers.tasks import (
    ai_triage,
    disk_gc,
    parse_crash,
    recover_crash_jobs,
    resymbolicate,
    run_crash_job,
    symbol_ingest,
)

logger = logging.getLogger(__name__)

TASK_REGISTRY: dict[str, Callable[..., Awaitable[Any]]] = {
    "symbol_ingest": symbol_ingest,
    "parse_crash": parse_crash,
    "ai_triage": ai_triage,
    "resymbolicate": resymbolicate,
    "disk_gc": disk_gc,
    "run_crash_job": run_crash_job,
}


def parse_redis_settings(redis_url: str | None = None) -> RedisSettings:
    """Parse REDIS_URL into ARQ RedisSettings, defaulting safely for memory://."""
    url = (redis_url if redis_url is not None else get_settings().REDIS_URL).strip()
    if not url or url.lower().startswith("memory://"):
        return RedisSettings()
    return RedisSettings.from_dsn(url)


async def startup(ctx: dict[str, Any]) -> None:
    """ARQ worker startup hook initializing DB engine, storage, and SSE EventBus."""
    settings = get_settings()
    init_db(settings.DATABASE_URL)
    ctx["settings"] = settings
    ctx["session_factory"] = get_session_factory()
    ctx["storage"] = StorageManager(data_dir=settings.DATA_DIR, settings=settings)
    ctx["event_bus"] = get_event_bus(settings)


async def shutdown(ctx: dict[str, Any]) -> None:
    """ARQ worker shutdown hook releasing EventBus and DB connections."""
    event_bus = ctx.get("event_bus")
    if event_bus is not None:
        await event_bus.close()
    await close_event_bus()
    await dispose_engine()


class WorkerSettings:
    """ARQ WorkerSettings consumed by `arq app.workers.worker.WorkerSettings`."""

    redis_settings = parse_redis_settings()
    functions = [
        symbol_ingest, parse_crash, ai_triage, resymbolicate, disk_gc,
        func(run_crash_job, keep_result=0),
    ]
    cron_jobs = [
        cron(disk_gc, minute={0, 15, 30, 45}),
        cron(recover_crash_jobs, second=set(range(0, 60, 5)), run_at_startup=True),
    ]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 8
    job_timeout = CRASH_JOB_TIMEOUT_SECONDS
    keep_result = 300


class TaskDispatcher:
    """Unified task dispatcher supporting ARQ Redis and tracked in-process memory:// execution."""

    def __init__(
        self,
        settings: Settings,
        storage: StorageManager,
        event_bus: EventBus,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        arq_pool: ArqRedis | None = None,
        exec_fn: Any = None,
        openai_client: Any = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.event_bus = event_bus
        self.session_factory = session_factory
        self.arq_pool = arq_pool
        self._owns_pool = False
        self._pool_lock = asyncio.Lock()
        self.exec_fn = exec_fn
        self.openai_client = openai_client
        self._memory_tasks: set[asyncio.Task[Any]] = set()
        self._memory_job_ids: set[str] = set()
        self._memory_job_slots = asyncio.Semaphore(WorkerSettings.max_jobs)
        self._memory_recovery: asyncio.Task[None] | None = None

    @property
    def is_memory_mode(self) -> bool:
        return (self.settings.REDIS_URL or "").strip().lower().startswith("memory://")

    @property
    def pending_count(self) -> int:
        return len(self._memory_tasks)

    async def _run_memory_task(
        self,
        fn: Callable[..., Awaitable[Any]],
        ctx: dict[str, Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        try:
            if fn is run_crash_job:
                async with self._memory_job_slots:
                    return await fn(ctx, *args, **kwargs)
            return await fn(ctx, *args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unhandled error in memory:// background task %s", fn.__name__)
            return None

    async def enqueue(self, task_name: str, *args: Any, **kwargs: Any) -> str | None:
        """Enqueue a named task via tracked asyncio.create_task (memory://) or ARQ Redis."""
        if task_name not in TASK_REGISTRY:
            raise ValueError(f"Unknown worker task: {task_name!r}")

        if self.is_memory_mode:
            fn = TASK_REGISTRY[task_name]
            job_id = str(args[0]) if task_name == "run_crash_job" else None
            if job_id is not None and job_id in self._memory_job_ids:
                return None
            ctx: dict[str, Any] = {
                "settings": self.settings,
                "storage": self.storage,
                "session_factory": self.session_factory or get_session_factory(),
                "event_bus": self.event_bus,
                "dispatcher": self,
            }
            if self.exec_fn is not None:
                ctx["exec_fn"] = self.exec_fn
            if self.openai_client is not None:
                ctx["openai_client"] = self.openai_client

            task = asyncio.create_task(
                self._run_memory_task(fn, ctx, *args, **kwargs),
                name=f"crashkiller-task-{task_name}",
            )
            self._memory_tasks.add(task)
            task.add_done_callback(self._memory_tasks.discard)
            if job_id is not None:
                self._memory_job_ids.add(job_id)
                task.add_done_callback(lambda _: self._memory_job_ids.discard(job_id))
            return task.get_name()

        pool = await self._get_arq_pool()
        job = await pool.enqueue_job(task_name, *args, **kwargs)
        return getattr(job, "job_id", None) if job is not None else None

    async def _get_arq_pool(self) -> ArqRedis:
        if self.arq_pool is None:
            async with self._pool_lock:
                if self.arq_pool is None:
                    self.arq_pool = await create_pool(parse_redis_settings(self.settings.REDIS_URL))
                    self._owns_pool = True
        return self.arq_pool

    async def kick_crash_jobs(self, job_ids: Iterable[uuid.UUID]) -> None:
        """Try immediate delivery. A persisted job remains visible to the recovery loop."""
        try:
            async with asyncio.timeout(JOB_DELIVERY_TIMEOUT_SECONDS):
                for job_id in job_ids:
                    if self.is_memory_mode:
                        await self.enqueue("run_crash_job", str(job_id))
                    else:
                        await enqueue_crash_job(await self._get_arq_pool(), job_id)
        except Exception:
            logger.warning("Crash jobs remain pending after delivery failure", exc_info=True)

    def start_memory_recovery(self) -> None:
        if self.is_memory_mode and self._memory_recovery is None:
            self._memory_recovery = asyncio.create_task(self._recover_memory_jobs())

    async def _recover_memory_jobs(self) -> None:
        factory = self.session_factory or get_session_factory()
        while True:
            try:
                async with factory() as session:
                    job_ids = await pending_crash_jobs(session)
                await self.kick_crash_jobs(job_ids)
            except Exception:
                logger.warning("Could not recover pending crash jobs", exc_info=True)
            await asyncio.sleep(5)

    async def drain_and_cancel(self, timeout: float = 5.0) -> None:
        """Await pending memory tasks up to timeout, cancel remaining, and close owned pool."""
        if self._memory_recovery is not None:
            self._memory_recovery.cancel()
            await asyncio.gather(self._memory_recovery, return_exceptions=True)
            self._memory_recovery = None
        while self._memory_tasks:
            pending = list(self._memory_tasks)
            done, still_pending = await asyncio.wait(pending, timeout=timeout)
            if still_pending:
                for task in still_pending:
                    task.cancel()
                await asyncio.gather(*still_pending, return_exceptions=True)
                break
            # Re-check in case a finished task spawned a chained task
            # (e.g. parse_crash -> ai_triage).
            if not self._memory_tasks:
                break

        if self._owns_pool and self.arq_pool is not None:
            aclose = getattr(self.arq_pool, "aclose", None)
            if callable(aclose):
                await aclose()
            else:
                close_fn = getattr(self.arq_pool, "close", None)
                if callable(close_fn):
                    await close_fn()
            self.arq_pool = None
            self._owns_pool = False
