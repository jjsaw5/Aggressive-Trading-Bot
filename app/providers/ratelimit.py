"""Proactive rate limiting, request budgeting, and priority for live providers.

The old behavior was purely reactive: hammer the API and back off only after a
429. This module stays UNDER the documented quotas instead:

- `TokenBucket` — one per provider, refilling at the documented req/min so bursts
  are smoothed before they trip a 429.
- `Priority` + `current_priority` — a context variable so higher-tier work
  (open positions > candidates > watchlist > broad scan) claims scarce tokens
  first. Nothing sets a non-default priority yet; the tier evaluators will, and
  the bucket already honors it.
- `RequestBudget` — an opt-in hard daily cap per provider, a safety kill-switch
  against runaway consumption.

Wired at the single HTTP choke point (`AsyncHTTP`), so every provider benefits
and the mock stack is untouched.
"""

from __future__ import annotations

import asyncio
import heapq
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from enum import IntEnum
from functools import lru_cache

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)


class Priority(IntEnum):
    """Lower value = higher priority (served first under contention)."""

    POSITIONS = 0  # Tier 4 — capital at risk, always first
    CANDIDATES = 1  # Tier 3
    WATCHLIST = 2  # Tier 2 (default)
    BROAD = 3  # Tier 1 — broad universe, lowest priority


DEFAULT_PRIORITY = int(Priority.WATCHLIST)
current_priority: ContextVar[int] = ContextVar("current_priority", default=DEFAULT_PRIORITY)


@contextmanager
def use_priority(priority: Priority | int):
    """Run a block at a given request priority (propagates to provider calls)."""
    token = current_priority.set(int(priority))
    try:
        yield
    finally:
        current_priority.reset(token)


# Documented per-minute limits (conservative). Providers not listed use
# settings.rate_limit_default_rpm.
DEFAULT_RPM: dict[str, int] = {
    "fmp": 250,
    "unusual_whales": 120,
    "robinhood": 60,
}


class BudgetExceededError(RuntimeError):
    def __init__(self, provider: str, cap: int) -> None:
        super().__init__(f"[{provider}] daily request budget exhausted (cap={cap})")
        self.provider = provider


class TokenBucket:
    """Async token bucket with priority-ordered waiters.

    `clock`/`sleep` are injectable so the refill math is deterministically
    testable without real time.
    """

    def __init__(
        self,
        rate_per_min: int,
        capacity: float | None = None,
        *,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ) -> None:
        self.rate = max(rate_per_min, 1) / 60.0  # tokens/sec
        self.capacity = capacity if capacity is not None else float(max(rate_per_min, 1))
        self.tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._heap: list[tuple[int, int]] = []
        self._seq = 0
        self.waited_count = 0  # metric: acquires that had to wait

    def _refill(self) -> None:
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._updated) * self.rate)
        self._updated = now

    async def acquire(self, priority: int = DEFAULT_PRIORITY) -> float:
        """Block until a token is available for this priority. Returns wait time."""
        seq = self._seq
        self._seq += 1
        ticket = (int(priority), seq)
        heapq.heappush(self._heap, ticket)
        waited = 0.0
        try:
            while True:
                self._refill()
                if self._heap and self._heap[0] == ticket and self.tokens >= 1.0:
                    heapq.heappop(self._heap)
                    self.tokens -= 1.0
                    if waited > 0:
                        self.waited_count += 1
                    return waited
                # Either not our turn (a higher-priority ticket is ahead) or not
                # enough tokens yet.
                if self._heap and self._heap[0] == ticket:
                    need = 1.0 - self.tokens
                    delay = max(need / self.rate, 0.001)
                else:
                    delay = 0.005
                delay = min(delay, 0.25)
                await self._sleep(delay)
                waited += delay
        except BaseException:
            try:
                self._heap.remove(ticket)
                heapq.heapify(self._heap)
            except ValueError:
                pass
            raise


class RequestBudget:
    """Per-provider daily request counter with a hard cap (resets at UTC day)."""

    def __init__(self, daily_cap: int, *, today=lambda: datetime.now(UTC).date()) -> None:
        self.daily_cap = daily_cap
        self._today_fn = today
        self._day = today()
        self.count = 0
        self.blocked = 0

    def charge(self, n: int = 1) -> None:
        today = self._today_fn()
        if today != self._day:
            self._day = today
            self.count = 0
        if self.count + n > self.daily_cap:
            self.blocked += 1
            raise BudgetExceededError("provider", self.daily_cap)
        self.count += n


class ProviderLimiter:
    """Facade: token bucket + optional daily budget, per provider."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        budget_enabled: bool = False,
        rpm: dict[str, int] | None = None,
        default_rpm: int = 120,
        daily_budget: int = 10_000,
    ) -> None:
        self.enabled = enabled
        self.budget_enabled = budget_enabled
        self._rpm = rpm or DEFAULT_RPM
        self._default_rpm = default_rpm
        self._daily_budget = daily_budget
        self._buckets: dict[str, TokenBucket] = {}
        self._budgets: dict[str, RequestBudget] = {}

    def _bucket(self, provider: str) -> TokenBucket:
        b = self._buckets.get(provider)
        if b is None:
            b = TokenBucket(self._rpm.get(provider, self._default_rpm))
            self._buckets[provider] = b
        return b

    def _budget(self, provider: str) -> RequestBudget:
        bud = self._budgets.get(provider)
        if bud is None:
            bud = RequestBudget(self._daily_budget)
            self._budgets[provider] = bud
        return bud

    async def acquire(self, provider: str, priority: int = DEFAULT_PRIORITY) -> None:
        """Acquire permission to make ONE real request. Called on cache miss."""
        if not self.enabled:
            return
        await self._bucket(provider).acquire(priority)
        if self.budget_enabled:
            try:
                self._budget(provider).charge()
            except BudgetExceededError as exc:
                log.error("api_budget_exceeded", provider=provider)
                raise exc

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "budget_enabled": self.budget_enabled,
            "buckets": {
                p: {"tokens": round(b.tokens, 2), "waited": b.waited_count}
                for p, b in self._buckets.items()
            },
            "budgets": {p: bud.count for p, bud in self._budgets.items()},
        }


@lru_cache
def get_limiter() -> ProviderLimiter:
    return ProviderLimiter(
        enabled=settings.rate_limit_enabled,
        budget_enabled=settings.api_budget_enabled,
        default_rpm=settings.rate_limit_default_rpm,
        daily_budget=settings.api_daily_budget,
    )
