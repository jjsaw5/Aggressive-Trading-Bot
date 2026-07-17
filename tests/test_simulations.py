"""Scenario + load simulations: market-open cadence progression, provider
outage isolation, rate-limit/budget shedding, earnings session, high-flow burst,
and a large-universe funnel load test. Each asserts observable behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import app.scheduling.engine as engine_mod
from app.domain.options import FlowAlert
from app.engine.universe import UniverseConfig
from app.events.bus import get_event_bus
from app.events.detectors import FlowBurstDetector
from app.events.types import EventType
from app.observability.metrics import get_metrics
from app.providers.mock import MockProvider
from app.scheduling.clock import MarketClock, MarketSession
from app.scheduling.engine import SessionScheduler
from app.scheduling.schedule import DEFAULT_SCHEDULE, TierSchedule
from app.tiers.models import Tier
from app.tiers.tier1_broad import Tier1BroadScanner

_ET = ZoneInfo("America/New_York")


class _CountingFunnel:
    def __init__(self) -> None:
        self.runs = {Tier.BROAD: 0, Tier.WATCHLIST: 0, Tier.CANDIDATES: 0, Tier.POSITIONS: 0}

    async def run_tier1(self):
        self.runs[Tier.BROAD] += 1

    async def run_tier2(self):
        self.runs[Tier.WATCHLIST] += 1

    async def run_tier3(self):
        self.runs[Tier.CANDIDATES] += 1

    async def run_tier4(self):
        self.runs[Tier.POSITIONS] += 1


# --- Market-open / power-hour: responsiveness scales with the session ---------
async def test_primary_window_positions_run_far_more_than_broad() -> None:
    sched = SessionScheduler(clock=MarketClock(), schedule=TierSchedule(DEFAULT_SCHEDULE))
    funnel = _CountingFunnel()
    primary = datetime(2026, 7, 16, 10, 0, tzinfo=_ET)  # PRIMARY
    # 120 seconds of 10s ticks.
    for i in range(13):
        await sched.tick(funnel, now=primary, mono=1000.0 + i * 10)
    # positions cadence 20s -> ~6 runs; broad 300s -> 1 run over 120s.
    assert funnel.runs[Tier.POSITIONS] >= 5
    assert funnel.runs[Tier.BROAD] == 1
    assert funnel.runs[Tier.POSITIONS] > funnel.runs[Tier.WATCHLIST] > funnel.runs[Tier.BROAD]


# --- Provider outage: tier failure isolated + ProviderFailure event ----------
async def test_provider_outage_isolated_and_emits_event() -> None:
    bus = get_event_bus()
    bus.reset()
    seen: list[str] = []

    async def cap(e):
        if e.type == EventType.PROVIDER_FAILURE:
            seen.append(e.payload.get("provider", ""))

    bus.subscribe_all(cap)

    class OutageFunnel(_CountingFunnel):
        async def run_tier1(self):
            raise RuntimeError("FMP 503")

    sched = SessionScheduler(clock=MarketClock(), schedule=TierSchedule(DEFAULT_SCHEDULE))
    before = get_metrics()._counters.get("scheduler.tier_failures", 0)
    ran = await sched.tick(OutageFunnel(), now=datetime(2026, 7, 16, 10, 0, tzinfo=_ET), mono=1.0)
    assert Tier.BROAD not in ran  # failed
    assert {Tier.WATCHLIST, Tier.CANDIDATES, Tier.POSITIONS} <= set(ran)  # others survived
    assert any("tier:BROAD" in p for p in seen)  # failure surfaced as an event
    assert get_metrics()._counters.get("scheduler.tier_failures", 0) > before
    bus.reset()


# --- Rate-limit / budget exhaustion: shed low-priority tiers first -----------
async def test_budget_exhaustion_sheds_low_priority_tiers(monkeypatch) -> None:
    monkeypatch.setattr(engine_mod.settings, "api_budget_enabled", True)
    monkeypatch.setattr(engine_mod.settings, "api_daily_budget", 100)

    class StubLimiter:
        def stats(self):
            return {"budgets": {"fmp": 100}}  # fully spent

    monkeypatch.setattr(engine_mod, "get_limiter", lambda: StubLimiter())

    sched = SessionScheduler(clock=MarketClock(), schedule=TierSchedule(DEFAULT_SCHEDULE))
    assert sched._budget_pressure() is True
    assert sched._allowed(Tier.BROAD) is False
    assert sched._allowed(Tier.WATCHLIST) is False
    assert sched._allowed(Tier.CANDIDATES) is True  # protected
    assert sched._allowed(Tier.POSITIONS) is True  # protected

    funnel = _CountingFunnel()
    ran = await sched.tick(funnel, now=datetime(2026, 7, 16, 10, 0, tzinfo=_ET), mono=1.0)
    assert Tier.BROAD not in ran and Tier.WATCHLIST not in ran
    assert Tier.CANDIDATES in ran and Tier.POSITIONS in ran


# --- Earnings session: after-hours tiers active, no candidates ---------------
def test_earnings_session_active_tiers() -> None:
    clock = MarketClock()
    sched = TierSchedule(DEFAULT_SCHEDULE)
    # 18:00 ET on a trading day is the EARNINGS window.
    assert clock.session(datetime(2026, 7, 16, 18, 0, tzinfo=_ET)) == MarketSession.EARNINGS
    active = set(sched.active_tiers(MarketSession.EARNINGS))
    assert Tier.POSITIONS in active and Tier.BROAD in active
    assert Tier.CANDIDATES not in active  # no fresh candidate discovery after hours


# --- High-flow burst: detector fires FlowDetected onto the bus ---------------
async def test_high_flow_burst_publishes_event() -> None:
    bus = get_event_bus()
    bus.reset()
    got: list[EventType] = []

    async def cap(e):
        got.append(e.type)

    bus.subscribe_all(cap)

    det = FlowBurstDetector(min_premium_usd=500_000)
    t0 = datetime(2026, 7, 16, 15, 0, tzinfo=UTC)
    det.detect("NVDA", [FlowAlert(symbol="NVDA", premium=100_000, ts=t0, source="x")])  # baseline
    evt = det.detect(
        "NVDA",
        [FlowAlert(symbol="NVDA", premium=750_000, ts=t0 + timedelta(minutes=1), source="x")],
    )
    assert evt is not None
    await bus.publish(evt)
    assert EventType.FLOW_DETECTED in got
    bus.reset()


# --- Load: sweep a large universe through Tier 1 --------------------------
async def test_tier1_load_over_large_universe() -> None:
    mock = MockProvider()
    universe = [f"SYM{i:04d}" for i in range(300)]
    t1 = Tier1BroadScanner(
        market=mock, fundamentals=mock, calendar=mock,
        universe=UniverseConfig(symbols=universe), concurrency=16,
    )
    results = await t1.run()
    assert len(results) == 300  # every symbol evaluated, none dropped
    # Sorted by score; bounded concurrency didn't corrupt ordering.
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)
