"""Async event bus: dispatch, per-type vs all, error isolation, unsubscribe."""

from __future__ import annotations

from app.events.bus import EventBus
from app.events.types import Event, EventType, price_changed


async def test_publish_reaches_type_and_all_subscribers() -> None:
    bus = EventBus()
    seen_type: list[Event] = []
    seen_all: list[Event] = []

    async def on_type(e: Event) -> None:
        seen_type.append(e)

    async def on_all(e: Event) -> None:
        seen_all.append(e)

    bus.subscribe(EventType.PRICE_CHANGED, on_type)
    bus.subscribe_all(on_all)

    n = await bus.publish(price_changed("AAPL", 100, 102, 2.0))
    assert n == 2
    assert len(seen_type) == 1 and len(seen_all) == 1


async def test_wrong_type_not_delivered() -> None:
    bus = EventBus()
    hits: list[Event] = []

    async def handler(e: Event) -> None:
        hits.append(e)

    bus.subscribe(EventType.FLOW_DETECTED, handler)
    await bus.publish(price_changed("AAPL", 100, 102, 2.0))  # different type
    assert hits == []


async def test_failing_subscriber_is_isolated() -> None:
    bus = EventBus()
    good: list[Event] = []

    async def boom(_e: Event) -> None:
        raise RuntimeError("handler blew up")

    async def ok(e: Event) -> None:
        good.append(e)

    bus.subscribe_all(boom)
    bus.subscribe_all(ok)
    handled = await bus.publish(price_changed("X", 1, 2, 100.0))
    assert handled == 1  # only the good one
    assert len(good) == 1
    assert bus.errors == 1 and bus.published == 1


async def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    hits: list[Event] = []

    async def handler(e: Event) -> None:
        hits.append(e)

    unsub = bus.subscribe(EventType.PRICE_CHANGED, handler)
    await bus.publish(price_changed("A", 1, 2, 100.0))
    unsub()
    await bus.publish(price_changed("A", 2, 3, 50.0))
    assert len(hits) == 1  # only the first got through


async def test_stats_counts_by_type() -> None:
    bus = EventBus()
    await bus.publish(price_changed("A", 1, 2, 100.0))
    await bus.publish(price_changed("B", 1, 2, 100.0))
    s = bus.stats()
    assert s["published"] == 2
    assert s["by_type"]["price_changed"] == 2
