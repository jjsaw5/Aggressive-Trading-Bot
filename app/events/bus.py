"""In-process async event bus (pub/sub) with error isolation and metrics.

Publishers `await bus.publish(event)` (or fire-and-forget via `bus.emit`).
Subscribers register per event type or for all events. A failing subscriber is
isolated — it is logged and never breaks other subscribers or the publisher.

Dispatch is direct (fan out to handlers concurrently) rather than queued: it is
deterministic to test and adequate for in-process handlers. A Redis-backed
transport can implement the same `publish`/`subscribe` surface later without
touching call sites.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from functools import lru_cache

from app.events.types import Event, EventType
from app.logging_config import get_logger

log = get_logger(__name__)

Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[EventType, list[Handler]] = defaultdict(list)
        self._all: list[Handler] = []
        self.published = 0
        self.handled = 0
        self.errors = 0
        self._by_type: dict[str, int] = defaultdict(int)

    def subscribe(self, event_type: EventType, handler: Handler) -> Callable[[], None]:
        """Register a handler for one event type. Returns an unsubscribe fn."""
        self._subs[event_type].append(handler)

        def _unsub() -> None:
            try:
                self._subs[event_type].remove(handler)
            except ValueError:
                pass

        return _unsub

    def subscribe_all(self, handler: Handler) -> Callable[[], None]:
        """Register a handler for every event type. Returns an unsubscribe fn."""
        self._all.append(handler)

        def _unsub() -> None:
            try:
                self._all.remove(handler)
            except ValueError:
                pass

        return _unsub

    async def _safe(self, handler: Handler, event: Event) -> int:
        try:
            await handler(event)
            self.handled += 1
            return 1
        except Exception as exc:  # noqa: BLE001 - a bad subscriber must not spread
            self.errors += 1
            log.warning(
                "event_handler_failed", type=event.type.value, error=str(exc)
            )
            return 0

    async def publish(self, event: Event) -> int:
        """Dispatch to all matching handlers. Returns how many handled cleanly."""
        self.published += 1
        self._by_type[event.type.value] += 1
        handlers = [*self._subs.get(event.type, ()), *self._all]
        if not handlers:
            return 0
        results = await asyncio.gather(*(self._safe(h, event) for h in handlers))
        return sum(results)

    def emit(self, event: Event) -> asyncio.Task:
        """Fire-and-forget publish (schedules a task on the running loop)."""
        return asyncio.create_task(self.publish(event))

    def stats(self) -> dict:
        return {
            "published": self.published,
            "handled": self.handled,
            "errors": self.errors,
            "subscribers": sum(len(v) for v in self._subs.values()) + len(self._all),
            "by_type": dict(self._by_type),
        }

    def reset(self) -> None:
        """Clear subscribers + metrics (used by tests)."""
        self._subs.clear()
        self._all.clear()
        self.published = self.handled = self.errors = 0
        self._by_type.clear()


@lru_cache
def get_event_bus() -> EventBus:
    return EventBus()
