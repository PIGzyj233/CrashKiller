"""Unit tests for InMemoryEventBus, RedisEventBus, and event bus lifecycle."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest

from app.config import get_settings
from app.services.events import (
    InMemoryEventBus,
    RedisEventBus,
    close_event_bus,
    create_event_bus,
    get_event_bus,
    set_event_bus,
)


async def test_event_bus_factory_and_rebind(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        assert isinstance(create_event_bus("memory://"), InMemoryEventBus)
        assert isinstance(create_event_bus("redis://localhost:6379/0"), RedisEventBus)
        with pytest.raises(ValueError):
            create_event_bus("amqp://localhost/")

        monkeypatch.setenv("REDIS_URL", "memory://")
        get_settings.cache_clear()
        set_event_bus(None)
        assert isinstance(get_event_bus(), InMemoryEventBus)

        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/1")
        get_settings.cache_clear()
        assert isinstance(get_event_bus(), RedisEventBus)
    finally:
        await close_event_bus()
        get_settings.cache_clear()


async def test_in_memory_event_bus_fanout_isolation_eviction_and_close() -> None:
    bus = InMemoryEventBus(max_queue_size=2)
    cid1 = uuid.uuid4()
    cid2 = uuid.uuid4()

    async with (
        bus.subscribe(cid1) as sub1a,
        bus.subscribe(cid1) as sub1b,
        bus.subscribe(cid2) as sub2,
    ):
        for st in ("PENDING", "PARSING"):
            await bus.publish_status(cid1, st, stage=st.lower())
        await bus.publish_status(cid2, "ANALYZED")

        e1a = [await sub1a.get(timeout=1.0), await sub1a.get(timeout=1.0)]
        e1b = [await sub1b.get(timeout=1.0), await sub1b.get(timeout=1.0)]
        e2 = await sub2.get(timeout=1.0)
        assert [e["status"] for e in e1a] == ["PENDING", "PARSING"]
        assert [e["status"] for e in e1b] == ["PENDING", "PARSING"]
        assert e2["crash_id"] == str(cid2)

        # Full queue drops oldest without blocking publish.
        await bus.publish_status(cid1, "PARSING", stage="s1")
        await bus.publish_status(cid1, "TRIAGING", stage="s2")
        await bus.publish_status(cid1, "ANALYZED", stage="s3")
        ev_kept = [await sub1a.get(timeout=1.0), await sub1a.get(timeout=1.0)]
        assert [e["status"] for e in ev_kept] == ["TRIAGING", "ANALYZED"]

    # After context exit, subscriber queues are cleaned up.
    assert not bus._subscribers

    # close() terminates active async iterator cleanly.
    sub_close = bus.subscribe(cid1)
    await bus.publish_status(cid1, "PARSING")
    await bus.close()
    collected = [ev["status"] async for ev in sub_close]
    assert collected == ["PARSING"]


async def test_redis_event_bus_publish_and_subscribe_with_mock_client() -> None:
    published: list[tuple[str, str]] = []
    unsubscribed: list[str] = []
    closed_pubsub = False

    class FakePubSub:
        def __init__(self) -> None:
            self.msgs: list[dict[str, Any]] = []

        async def subscribe(self, channel: str) -> None:
            self.msgs.append(
                {
                    "type": "message",
                    "data": json.dumps(
                        {
                            "crash_id": channel.split(":")[2],
                            "status": "ANALYZED",
                            "stage": "done",
                            "ai_verdict": "NEW_ISSUE",
                            "error": None,
                        }
                    ),
                }
            )

        async def get_message(self, **_kwargs: Any) -> dict[str, Any] | None:
            return self.msgs.pop(0) if self.msgs else None

        async def unsubscribe(self, channel: str) -> None:
            unsubscribed.append(channel)

        async def aclose(self) -> None:
            nonlocal closed_pubsub
            closed_pubsub = True

    fake_pubsub = FakePubSub()

    class FakeRedis:
        async def publish(self, channel: str, msg: str) -> int:
            published.append((channel, msg))
            return 1

        def pubsub(self) -> FakePubSub:
            return fake_pubsub

    bus = RedisEventBus("redis://localhost:6379/0", redis_client=FakeRedis(), poll_interval=0.05)
    cid = uuid.uuid4()
    await bus.publish_status(cid, "PARSING", stage="parsing_dump")
    assert len(published) == 1
    assert json.loads(published[0][1])["status"] == "PARSING"

    async with bus.subscribe(cid) as sub:
        ev = await sub.get(timeout=1.0)
        assert ev["status"] == "ANALYZED"
        assert ev["ai_verdict"] == "NEW_ISSUE"

    assert len(unsubscribed) == 1
    assert closed_pubsub is True


async def test_redis_subscription_cleans_up_on_subscribe_cancel_and_error() -> None:
    async def _run_case(*, cancel: bool) -> None:
        aclose_calls = 0
        subscribe_started = asyncio.Event()

        class FailingPubSub:
            async def subscribe(self, _channel: str) -> None:
                subscribe_started.set()
                if cancel:
                    await asyncio.sleep(10)
                raise ConnectionError("redis down")

            async def unsubscribe(self, _channel: str) -> None:
                pass

            async def aclose(self) -> None:
                nonlocal aclose_calls
                aclose_calls += 1

        class FakeRedis:
            def pubsub(self) -> FailingPubSub:
                return FailingPubSub()

        bus = RedisEventBus(
            "redis://localhost:6379/0",
            redis_client=FakeRedis(),
            poll_interval=0.05,
        )
        cid = uuid.uuid4()

        async def _enter() -> None:
            async with bus.subscribe(cid):
                pass

        if cancel:
            task = asyncio.create_task(_enter())
            await subscribe_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ConnectionError):
                await _enter()

        assert aclose_calls == 1
        assert len(bus._active_subscriptions) == 0
        await bus.close()

    await _run_case(cancel=True)
    await _run_case(cancel=False)


async def test_redis_subscription_closes_pubsub_on_concurrent_bus_close() -> None:
    """bus.close() running while pubsub.subscribe() is in-flight must not leak pubsub."""
    aclose_calls = 0
    subscribe_entered = asyncio.Event()
    allow_subscribe_return = asyncio.Event()

    class RacingPubSub:
        async def subscribe(self, _channel: str) -> None:
            subscribe_entered.set()
            await allow_subscribe_return.wait()

        async def get_message(self, **_kwargs: Any) -> dict[str, Any] | None:
            return None

        async def unsubscribe(self, _channel: str) -> None:
            pass

        async def aclose(self) -> None:
            nonlocal aclose_calls
            aclose_calls += 1

    class FakeRedis:
        def pubsub(self) -> RacingPubSub:
            return RacingPubSub()

    bus = RedisEventBus("redis://localhost:6379/0", redis_client=FakeRedis(), poll_interval=0.05)
    cid = uuid.uuid4()

    collected: list[dict[str, Any]] = []

    async def _consume() -> None:
        async for ev in bus.iter_events(cid):
            collected.append(ev)

    consumer = asyncio.create_task(_consume())
    await subscribe_entered.wait()
    await bus.close()
    allow_subscribe_return.set()
    await consumer

    assert collected == []
    assert aclose_calls == 1
    assert len(bus._active_subscriptions) == 0
