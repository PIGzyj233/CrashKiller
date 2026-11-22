"""Real-time SSE event bus abstraction (InMemory for tests/memory:// and Redis PubSub)."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

import redis.asyncio as aioredis
from pydantic import BaseModel

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

VALID_CRASH_STATUSES: frozenset[str] = frozenset(
    {"PENDING", "PARSING", "TRIAGING", "ANALYZED", "FAILED"}
)
TERMINAL_CRASH_STATUSES: frozenset[str] = frozenset({"ANALYZED", "FAILED"})


def crash_channel(crash_id: uuid.UUID | str) -> str:
    """Return the canonical PubSub channel name for a crash report."""
    return f"crashkiller:crash:{crash_id}:events"


class CrashStatusEvent(BaseModel):
    """Canonical SSE status payload emitted during crash ingestion & AI triage."""

    crash_id: str
    status: str
    stage: str | None = None
    ai_verdict: str | None = None
    error: str | None = None


def normalize_event_payload(
    crash_id: uuid.UUID | str,
    event: CrashStatusEvent | dict[str, Any],
) -> dict[str, Any]:
    """Normalize event objects or dicts to the 5-key CrashStatusEvent dict."""
    cid = str(crash_id)
    if isinstance(event, CrashStatusEvent):
        data = event.model_dump()
        data["crash_id"] = cid
        return data
    if isinstance(event, dict):
        return {
            "crash_id": str(event.get("crash_id") or cid),
            "status": str(event.get("status") or ""),
            "stage": event.get("stage"),
            "ai_verdict": event.get("ai_verdict"),
            "error": event.get("error"),
        }
    raise TypeError(f"Unsupported event payload type: {type(event).__name__}")


class EventSubscription(ABC):
    """Async context manager and async iterator for crash SSE events."""

    @abstractmethod
    async def get(self, timeout: float | None = None) -> dict[str, Any]:
        """Wait for and return the next event dict."""

    @abstractmethod
    async def close(self) -> None:
        """Unsubscribe and release resources."""

    async def __aenter__(self) -> EventSubscription:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    def __aiter__(self) -> EventSubscription:
        return self

    async def __anext__(self) -> dict[str, Any]:
        return await self.get()


class EventBus(ABC):
    """Abstract PubSub event bus for crash status updates."""

    @abstractmethod
    async def publish(
        self,
        crash_id: uuid.UUID | str,
        event: CrashStatusEvent | dict[str, Any],
    ) -> None:
        """Publish an event to all subscribers of crash_id."""

    async def publish_status(
        self,
        crash_id: uuid.UUID | str,
        status: str,
        *,
        stage: str | None = None,
        ai_verdict: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Construct and publish a CrashStatusEvent payload."""
        payload = CrashStatusEvent(
            crash_id=str(crash_id),
            status=status,
            stage=stage,
            ai_verdict=ai_verdict,
            error=error,
        ).model_dump()
        await self.publish(crash_id, payload)
        return payload

    @abstractmethod
    def subscribe(self, crash_id: uuid.UUID | str) -> EventSubscription:
        """Create a subscription for crash_id."""

    async def iter_events(
        self,
        crash_id: uuid.UUID | str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield events for crash_id until closed or cancelled."""
        try:
            async with self.subscribe(crash_id) as sub:
                async for item in sub:
                    yield item
        except StopAsyncIteration:
            return

    @abstractmethod
    async def close(self) -> None:
        """Close the bus and terminate active subscriptions."""


class _InMemorySubscription(EventSubscription):
    """In-memory queue-backed subscription registered synchronously at creation."""

    def __init__(
        self,
        bus: InMemoryEventBus,
        channel: str,
        max_queue_size: int,
    ) -> None:
        self._bus = bus
        self._channel = channel
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=max(1, max_queue_size),
        )
        self._closed = False
        # Synchronously register queue so no events published immediately after
        # `bus.subscribe(crash_id)` are missed before `await`.
        if not bus._closed:
            bus._subscribers[channel].add(self._queue)
        else:
            self._queue.put_nowait(None)

    async def get(self, timeout: float | None = None) -> dict[str, Any]:
        if self._closed and self._queue.empty():
            raise StopAsyncIteration
        if timeout is None:
            item = await self._queue.get()
        else:
            item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        subs = self._bus._subscribers.get(self._channel)
        if subs is not None:
            subs.discard(self._queue)
            if not subs:
                self._bus._subscribers.pop(self._channel, None)
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


class InMemoryEventBus(EventBus):
    """Process-local async PubSub bus used when REDIS_URL=memory://."""

    def __init__(self, max_queue_size: int = 256) -> None:
        self._max_queue_size = max_queue_size
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any] | None]]] = defaultdict(set)
        self._closed = False

    async def publish(
        self,
        crash_id: uuid.UUID | str,
        event: CrashStatusEvent | dict[str, Any],
    ) -> None:
        if self._closed:
            return
        payload = normalize_event_payload(crash_id, event)
        channel = crash_channel(crash_id)
        for queue in list(self._subscribers.get(channel, ())):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(dict(payload))
            except asyncio.QueueFull:
                pass

    def subscribe(self, crash_id: uuid.UUID | str) -> EventSubscription:
        return _InMemorySubscription(
            bus=self,
            channel=crash_channel(crash_id),
            max_queue_size=self._max_queue_size,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for queues in list(self._subscribers.values()):
            for queue in list(queues):
                if queue.full():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
        self._subscribers.clear()


class _RedisSubscription(EventSubscription):
    """Redis PubSub-backed subscription."""

    def __init__(
        self,
        bus: RedisEventBus,
        channel: str,
        poll_interval: float,
    ) -> None:
        self._bus = bus
        self._channel = channel
        self._poll_interval = poll_interval
        self._pubsub: Any = None
        self._closed = False
        self._bus._active_subscriptions.add(self)

    @staticmethod
    async def _close_pubsub(pubsub: Any) -> None:
        aclose = getattr(pubsub, "aclose", None)
        if callable(aclose):
            res = aclose()
            if inspect.isawaitable(res):
                await res
            return
        close_fn = getattr(pubsub, "close", None)
        if callable(close_fn):
            res = close_fn()
            if inspect.isawaitable(res):
                await res

    async def _ensure_subscribed(self) -> Any:
        if self._closed or self._bus._closed:
            raise StopAsyncIteration
        if self._pubsub is None:
            client = self._bus._get_client()
            pubsub = client.pubsub()
            try:
                await pubsub.subscribe(self._channel)
            except BaseException:
                await self._close_pubsub(pubsub)
                raise
            if self._closed or self._bus._closed:
                await self._close_pubsub(pubsub)
                raise StopAsyncIteration
            self._pubsub = pubsub
        return self._pubsub

    async def __aenter__(self) -> EventSubscription:
        try:
            await self._ensure_subscribed()
        except BaseException:
            self._bus._active_subscriptions.discard(self)
            await self.close()
            raise
        return self

    async def get(self, timeout: float | None = None) -> dict[str, Any]:
        pubsub = await self._ensure_subscribed()
        deadline = None if timeout is None else time.monotonic() + timeout

        while not self._closed and not self._bus._closed:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for Redis PubSub event")
                step_timeout = min(self._poll_interval, remaining)
            else:
                step_timeout = self._poll_interval

            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=step_timeout,
            )
            if message is None:
                # Yield control briefly so fake/mock pubsubs returning None immediately
                # don't spin tightly without yielding to other coroutines.
                await asyncio.sleep(0)
                continue

            if message.get("type") not in (None, "message", "pmessage"):
                continue

            raw_data = message.get("data")
            if isinstance(raw_data, bytes):
                raw_data = raw_data.decode("utf-8", errors="replace")
            if isinstance(raw_data, str):
                try:
                    parsed = json.loads(raw_data)
                except json.JSONDecodeError:
                    logger.warning("Ignoring non-JSON event on channel %s", self._channel)
                    continue
            elif isinstance(raw_data, dict):
                parsed = raw_data
            else:
                continue

            if isinstance(parsed, dict):
                return parsed

        raise StopAsyncIteration

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus._active_subscriptions.discard(self)
        pubsub = self._pubsub
        self._pubsub = None
        if pubsub is not None:
            try:
                await pubsub.unsubscribe(self._channel)
            except Exception:
                logger.debug("Ignoring unsubscribe error on %s", self._channel, exc_info=True)
            finally:
                await self._close_pubsub(pubsub)


class RedisEventBus(EventBus):
    """Redis PubSub-backed event bus for multi-process API <-> ARQ worker SSE."""

    def __init__(
        self,
        redis_url: str,
        *,
        redis_client: Any = None,
        poll_interval: float = 0.5,
    ) -> None:
        self._redis_url = redis_url
        self._redis: Any = redis_client
        self._owns_client: bool = redis_client is None
        self._poll_interval = poll_interval
        self._active_subscriptions: set[_RedisSubscription] = set()
        self._closed = False

    def _get_client(self) -> Any:
        if self._redis is None:
            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
            self._owns_client = True
        return self._redis

    async def publish(
        self,
        crash_id: uuid.UUID | str,
        event: CrashStatusEvent | dict[str, Any],
    ) -> None:
        if self._closed:
            return
        payload = normalize_event_payload(crash_id, event)
        client = self._get_client()
        await client.publish(crash_channel(crash_id), json.dumps(payload))

    def subscribe(self, crash_id: uuid.UUID | str) -> EventSubscription:
        return _RedisSubscription(
            bus=self,
            channel=crash_channel(crash_id),
            poll_interval=self._poll_interval,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for sub in list(self._active_subscriptions):
            await sub.close()
        self._active_subscriptions.clear()
        if self._owns_client and self._redis is not None:
            aclose = getattr(self._redis, "aclose", None)
            if callable(aclose):
                await aclose()
            else:
                close_fn = getattr(self._redis, "close", None)
                if callable(close_fn):
                    await close_fn()
        self._redis = None


_event_bus: EventBus | None = None
_active_redis_url: str | None = None


def create_event_bus(
    redis_url: str | None = None,
    *,
    redis_client: Any = None,
) -> EventBus:
    """Instantiate the appropriate EventBus backend for a given REDIS_URL."""
    url = (redis_url if redis_url is not None else get_settings().REDIS_URL).strip()
    lower = url.lower()
    if lower.startswith("memory://"):
        return InMemoryEventBus()
    if lower.startswith(("redis://", "rediss://", "unix://")):
        return RedisEventBus(url, redis_client=redis_client)
    raise ValueError(f"Unsupported REDIS_URL scheme: {url!r}")


def get_event_bus(settings: Settings | None = None) -> EventBus:
    """Return the active singleton EventBus, rebinding if REDIS_URL changed."""
    global _event_bus, _active_redis_url
    cfg = settings or get_settings()
    if _event_bus is None or _active_redis_url != cfg.REDIS_URL:
        _event_bus = create_event_bus(cfg.REDIS_URL)
        _active_redis_url = cfg.REDIS_URL
    return _event_bus


def set_event_bus(bus: EventBus | None, redis_url: str | None = None) -> None:
    """Override or clear the module-level EventBus singleton (primarily for tests)."""
    global _event_bus, _active_redis_url
    _event_bus = bus
    if bus is None:
        _active_redis_url = None
    else:
        _active_redis_url = redis_url if redis_url is not None else get_settings().REDIS_URL


async def close_event_bus() -> None:
    """Close and clear the active EventBus singleton."""
    global _event_bus, _active_redis_url
    if _event_bus is not None:
        await _event_bus.close()
    _event_bus = None
    _active_redis_url = None
