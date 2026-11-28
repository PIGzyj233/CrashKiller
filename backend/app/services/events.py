"""Real-time SSE event bus (InMemory for memory:// and multiplexed Redis PubSub)."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
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
    """Queue-backed subscription multiplexed onto a single shared Redis PubSub connection."""

    def __init__(
        self,
        bus: RedisEventBus,
        channel: str,
        max_queue_size: int = 256,
    ) -> None:
        self._bus = bus
        self._channel = channel
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=max(1, max_queue_size),
        )
        self._registered = False
        self._closed = False

    def _enqueue(self, item: dict[str, Any] | None) -> None:
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            pass

    async def _ensure_registered(self) -> None:
        if self._closed or self._bus._closed:
            raise StopAsyncIteration
        if not self._registered:
            await self._bus._register_subscription(self)
            self._registered = True

    async def __aenter__(self) -> EventSubscription:
        try:
            await self._ensure_registered()
        except BaseException:
            await self.close()
            raise
        return self

    async def get(self, timeout: float | None = None) -> dict[str, Any]:
        if not self._registered and not self._closed:
            await self._ensure_registered()
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
        was_registered = self._registered
        self._registered = False
        if was_registered or self in self._bus._active_subscriptions:
            await self._bus._unregister_subscription(self)
        self._enqueue(None)


@dataclass
class _ChannelSetup:
    event: asyncio.Event = field(default_factory=asyncio.Event)
    succeeded: bool = False
    error: BaseException | None = None


class RedisEventBus(EventBus):
    """Multiplexed single-connection Redis PubSub event bus for API <-> Worker SSE."""

    def __init__(
        self,
        redis_url: str,
        *,
        redis_client: Any = None,
        poll_interval: float = 0.5,
        max_queue_size: int = 256,
    ) -> None:
        self._redis_url = redis_url
        self._redis: Any = redis_client
        self._owns_client: bool = redis_client is None
        self._poll_interval = max(0.01, float(poll_interval))
        self._max_queue_size = max(1, int(max_queue_size))
        self._active_subscriptions: set[_RedisSubscription] = set()
        self._channel_subscribers: dict[str, set[_RedisSubscription]] = defaultdict(set)
        self._channel_subscribed: set[str] = set()
        self._channel_ready: dict[str, _ChannelSetup] = {}
        self._pubsub: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._pubsub_op_lock = asyncio.Lock()
        self._pending_subscribes: int = 0
        self._closed = False

    def _get_client(self) -> Any:
        if self._redis is None:
            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
            self._owns_client = True
        return self._redis

    @staticmethod
    async def _close_pubsub_obj(pubsub: Any) -> None:
        if pubsub is None:
            return
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

    def _start_reader_locked(self) -> None:
        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(
                self._reader_loop(),
                name="crashkiller-redis-eventbus-reader",
            )

    async def _teardown_shared_pubsub_locked(self) -> None:
        reader = self._reader_task
        self._reader_task = None
        pubsub = self._pubsub
        self._pubsub = None
        self._channel_subscribed.clear()
        if reader is not None and not reader.done() and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await self._close_pubsub_obj(pubsub)

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        if message.get("type") not in (None, "message", "pmessage"):
            return
        raw_data = message.get("data")
        if isinstance(raw_data, bytes):
            raw_data = raw_data.decode("utf-8", errors="replace")
        if isinstance(raw_data, str):
            try:
                parsed = json.loads(raw_data)
            except json.JSONDecodeError:
                logger.warning("Ignoring non-JSON event on Redis PubSub")
                return
        elif isinstance(raw_data, dict):
            parsed = raw_data
        else:
            return
        if not isinstance(parsed, dict):
            return

        raw_channel = message.get("channel")
        if isinstance(raw_channel, bytes):
            channel = raw_channel.decode("utf-8", errors="replace")
        elif isinstance(raw_channel, str) and raw_channel:
            channel = raw_channel
        elif parsed.get("crash_id"):
            channel = crash_channel(str(parsed["crash_id"]))
        else:
            return

        for sub in list(self._channel_subscribers.get(channel, ())):
            sub._enqueue(dict(parsed))

    async def _reader_loop(self) -> None:
        backoff = 0.1
        try:
            while not self._closed and self._active_subscriptions:
                pubsub = self._pubsub
                if pubsub is None:
                    await asyncio.sleep(backoff)
                    backoff = min(2.0, backoff * 2.0)
                    if self._closed or not self._active_subscriptions:
                        return
                    reconnected = await self._reconnect_pubsub()
                    if reconnected:
                        backoff = 0.1
                    continue
                try:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=self._poll_interval,
                    )
                    backoff = 0.1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if self._closed or not self._active_subscriptions:
                        return
                    logger.warning(
                        "RedisEventBus reader error; reconnecting in %.2fs",
                        backoff,
                        exc_info=True,
                    )
                    async with self._state_lock:
                        broken = self._pubsub
                        self._pubsub = None
                        self._channel_subscribed.clear()
                        await self._close_pubsub_obj(broken)
                    await asyncio.sleep(backoff)
                    backoff = min(2.0, backoff * 2.0)
                    reconnected = await self._reconnect_pubsub()
                    if reconnected:
                        backoff = 0.1
                    continue

                if message is None:
                    await asyncio.sleep(0)
                    continue
                self._dispatch_message(message)
        except asyncio.CancelledError:
            raise

    async def _reconnect_pubsub(self) -> bool:
        async with self._pubsub_op_lock:
            async with self._state_lock:
                if self._closed or not self._active_subscriptions:
                    return False
                old = self._pubsub
                self._pubsub = None
                self._channel_subscribed.clear()
                await self._close_pubsub_obj(old)
                channels = list(self._channel_subscribers.keys())
                if not channels:
                    return False

            new_pubsub: Any = None
            try:
                client = self._get_client()
                new_pubsub = client.pubsub()
                for ch in channels:
                    await new_pubsub.subscribe(ch)
            except asyncio.CancelledError:
                await self._close_pubsub_obj(new_pubsub)
                raise
            except Exception:
                await self._close_pubsub_obj(new_pubsub)
                return False

            extra_channels: list[str] = []
            async with self._state_lock:
                if self._closed or not self._active_subscriptions:
                    await self._close_pubsub_obj(new_pubsub)
                    return False
                if self._pubsub is not None:
                    await self._close_pubsub_obj(new_pubsub)
                    return True
                subscribed = set(channels)
                extra_channels = [
                    ch for ch in self._channel_subscribers if ch not in subscribed
                ]
                if not extra_channels:
                    self._pubsub = new_pubsub
                    self._channel_subscribed = set(channels)
                    return True

            try:
                for ch in extra_channels:
                    await new_pubsub.subscribe(ch)
            except asyncio.CancelledError:
                await self._close_pubsub_obj(new_pubsub)
                raise
            except Exception:
                await self._close_pubsub_obj(new_pubsub)
                return False

            async with self._state_lock:
                if self._closed or not self._active_subscriptions:
                    await self._close_pubsub_obj(new_pubsub)
                    return False
                if self._pubsub is not None:
                    await self._close_pubsub_obj(new_pubsub)
                    return True
                self._pubsub = new_pubsub
                self._channel_subscribed = set(channels) | set(extra_channels)
            return True

    async def _register_subscription(self, sub: _RedisSubscription) -> None:
        channel = sub._channel

        while True:
            is_leader = False
            setup: _ChannelSetup | None = None
            pubsub: Any = None

            async with self._state_lock:
                if self._closed or sub._closed:
                    raise StopAsyncIteration
                self._active_subscriptions.add(sub)
                self._channel_subscribers[channel].add(sub)
                if channel in self._channel_subscribed and self._pubsub is not None:
                    self._start_reader_locked()
                    return
                setup = self._channel_ready.get(channel)
                if setup is None:
                    setup = _ChannelSetup()
                    self._channel_ready[channel] = setup
                    self._pending_subscribes += 1
                    is_leader = True

            if is_leader:
                assert setup is not None
                try:
                    # Serialize connection creation and SUBSCRIBE commands on the shared
                    # redis-py PubSub instance so concurrent first subscribers never open
                    # competing TCP connections and lose live messages.
                    async with self._pubsub_op_lock:
                        async with self._state_lock:
                            if self._closed or sub._closed:
                                raise StopAsyncIteration
                            if self._pubsub is None:
                                client = self._get_client()
                                self._pubsub = client.pubsub()
                            pubsub = self._pubsub
                        await pubsub.subscribe(channel)
                except BaseException as exc:
                    async with self._state_lock:
                        self._pending_subscribes = max(0, self._pending_subscribes - 1)
                        subs = self._channel_subscribers.get(channel)
                        if subs is not None:
                            subs.discard(sub)
                            if not subs:
                                self._channel_subscribers.pop(channel, None)
                        if self._channel_ready.get(channel) is setup:
                            self._channel_ready.pop(channel, None)
                        self._active_subscriptions.discard(sub)
                        if not isinstance(exc, asyncio.CancelledError):
                            setup.error = exc
                        setup.event.set()
                        if self._closed or (
                            not self._active_subscriptions and self._pending_subscribes == 0
                        ):
                            await self._teardown_shared_pubsub_locked()
                    raise

                should_unsub = False
                async with self._state_lock:
                    self._pending_subscribes = max(0, self._pending_subscribes - 1)
                    if self._pubsub is pubsub and not self._closed:
                        self._channel_subscribed.add(channel)
                        setup.succeeded = True
                    if self._channel_ready.get(channel) is setup:
                        self._channel_ready.pop(channel, None)
                    setup.event.set()
                    if self._closed or sub._closed:
                        subs = self._channel_subscribers.get(channel)
                        if subs is not None:
                            subs.discard(sub)
                            if not subs:
                                self._channel_subscribers.pop(channel, None)
                                self._channel_subscribed.discard(channel)
                                should_unsub = True
                        self._active_subscriptions.discard(sub)
                    else:
                        self._start_reader_locked()
                        return

                if should_unsub and pubsub is not None and not self._closed:
                    async with self._pubsub_op_lock:
                        if self._pubsub is pubsub and not self._closed:
                            try:
                                await pubsub.unsubscribe(channel)
                            except Exception:
                                pass
                async with self._state_lock:
                    if (
                        (self._closed or not self._active_subscriptions)
                        and self._pending_subscribes == 0
                    ):
                        await self._teardown_shared_pubsub_locked()
                raise StopAsyncIteration

            assert setup is not None
            try:
                await setup.event.wait()
            except BaseException:
                await self._unregister_subscription(sub)
                raise

            if setup.error is not None:
                await self._unregister_subscription(sub)
                raise setup.error

            if setup.succeeded:
                async with self._state_lock:
                    if self._closed or sub._closed:
                        subs = self._channel_subscribers.get(channel)
                        if subs is not None:
                            subs.discard(sub)
                            if not subs:
                                self._channel_subscribers.pop(channel, None)
                        self._active_subscriptions.discard(sub)
                        if not self._active_subscriptions and self._pending_subscribes == 0:
                            await self._teardown_shared_pubsub_locked()
                        raise StopAsyncIteration
                    if channel in self._channel_subscribed and self._pubsub is not None:
                        self._start_reader_locked()
                        return
            # Leader was cancelled before subscribing or pubsub reconnected; loop to promote
            # this waiter to leader and subscribe the channel itself.

    async def _unregister_subscription(self, sub: _RedisSubscription) -> None:
        channel = sub._channel
        should_unsub = False
        pubsub: Any = None
        async with self._state_lock:
            subs = self._channel_subscribers.get(channel)
            if subs is not None:
                subs.discard(sub)
                if not subs:
                    self._channel_subscribers.pop(channel, None)
                    self._channel_ready.pop(channel, None)
                    was_subscribed = channel in self._channel_subscribed
                    self._channel_subscribed.discard(channel)
                    should_unsub = was_subscribed
            self._active_subscriptions.discard(sub)
            pubsub = self._pubsub

        if should_unsub and pubsub is not None and not self._closed:
            async with self._pubsub_op_lock:
                if self._pubsub is pubsub and not self._closed:
                    try:
                        await pubsub.unsubscribe(channel)
                    except Exception:
                        logger.debug("Ignoring unsubscribe error on %s", channel, exc_info=True)

        async with self._state_lock:
            if not self._active_subscriptions and self._pending_subscribes == 0:
                await self._teardown_shared_pubsub_locked()

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
            max_queue_size=self._max_queue_size,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for setup in list(self._channel_ready.values()):
            setup.event.set()
        for sub in list(self._active_subscriptions):
            sub._closed = True
            sub._registered = False
            sub._enqueue(None)
        self._active_subscriptions.clear()
        self._channel_subscribers.clear()
        self._channel_subscribed.clear()
        self._channel_ready.clear()

        async with self._state_lock:
            if self._pending_subscribes == 0:
                await self._teardown_shared_pubsub_locked()
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
