"""Token bucket (rate + priority), request budget, and priority context."""

from __future__ import annotations

import asyncio

import pytest

from app.providers.ratelimit import (
    BudgetExceededError,
    Priority,
    RequestBudget,
    TokenBucket,
    current_priority,
    use_priority,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


async def _noop_sleep(_d: float) -> None:
    return None


async def test_bucket_consumes_then_refills_with_time() -> None:
    clock = _Clock()
    # 60/min = 1 token/sec, capacity 2.
    b = TokenBucket(rate_per_min=60, capacity=2, clock=clock, sleep=_noop_sleep)
    assert await b.acquire() == 0.0  # token 1 (no wait)
    assert await b.acquire() == 0.0  # token 2 (no wait)
    # Empty now; acquiring waits. With no-op sleep + frozen clock it would spin,
    # so advance the clock inside the sleep to simulate refill.
    async def advancing_sleep(d: float) -> None:
        clock.t += d

    b._sleep = advancing_sleep
    waited = await b.acquire()
    assert waited > 0  # had to wait for a refill
    assert clock.t >= 1.0  # ~1s to regenerate one token at 1/sec


async def test_budget_charges_and_caps() -> None:
    day = {"d": "2026-07-17"}
    bud = RequestBudget(daily_cap=3, today=lambda: day["d"])
    bud.charge()
    bud.charge()
    bud.charge()
    with pytest.raises(BudgetExceededError):
        bud.charge()
    assert bud.count == 3 and bud.blocked == 1


async def test_budget_resets_on_new_day() -> None:
    day = {"d": "2026-07-17"}
    bud = RequestBudget(daily_cap=1, today=lambda: day["d"])
    bud.charge()
    with pytest.raises(BudgetExceededError):
        bud.charge()
    day["d"] = "2026-07-18"  # new day
    bud.charge()  # resets, allowed again
    assert bud.count == 1


def test_priority_context_manager() -> None:
    assert current_priority.get() == int(Priority.WATCHLIST)
    with use_priority(Priority.POSITIONS):
        assert current_priority.get() == int(Priority.POSITIONS)
    assert current_priority.get() == int(Priority.WATCHLIST)  # restored


async def test_higher_priority_served_first_under_contention() -> None:
    # 600/min = 10 tokens/sec (100ms/token), capacity 1. Drain, then two waiters
    # contend for the next token; the high-priority one must win regardless of
    # arrival order.
    b = TokenBucket(rate_per_min=600, capacity=1)
    await b.acquire(int(Priority.WATCHLIST))  # drain the initial token
    order: list[str] = []

    async def worker(priority: Priority, label: str) -> None:
        await b.acquire(int(priority))
        order.append(label)

    low = asyncio.create_task(worker(Priority.BROAD, "low"))
    await asyncio.sleep(0.01)  # ensure low registers first
    high = asyncio.create_task(worker(Priority.POSITIONS, "high"))
    await asyncio.gather(low, high)
    assert order[0] == "high"  # priority beat arrival order
