"""Declarative schedule loading + the session scheduler's due/throttle logic."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.scheduling.clock import MarketClock, MarketSession
from app.scheduling.engine import SessionScheduler
from app.scheduling.schedule import DEFAULT_SCHEDULE, TierSchedule, load_schedule
from app.tiers.models import Tier

_ET = ZoneInfo("America/New_York")


def test_load_schedule_from_yaml_file() -> None:
    sched = load_schedule()  # config/scheduling.yaml
    # Primary window: fast watchlist + positions, slower broad.
    assert sched.cadence(MarketSession.PRIMARY, Tier.POSITIONS) == 20
    assert sched.cadence(MarketSession.PRIMARY, Tier.WATCHLIST) == 60
    assert sched.cadence(MarketSession.PRIMARY, Tier.BROAD) == 300
    # Overnight: broad only, watchlist/candidates disabled.
    assert sched.cadence(MarketSession.OVERNIGHT, Tier.WATCHLIST) is None
    assert sched.cadence(MarketSession.OVERNIGHT, Tier.BROAD) == 3600


def test_closed_session_runs_only_broad() -> None:
    sched = TierSchedule(DEFAULT_SCHEDULE)
    active = sched.active_tiers(MarketSession.CLOSED)
    assert active == [Tier.BROAD]


def test_due_tiers_respects_cadence() -> None:
    sched = SessionScheduler(clock=MarketClock(), schedule=TierSchedule(DEFAULT_SCHEDULE))
    # First tick: everything active in PRIMARY is due (no last-run yet).
    due0 = sched.due_tiers(MarketSession.PRIMARY, mono=1000.0)
    assert set(due0) == {Tier.BROAD, Tier.WATCHLIST, Tier.CANDIDATES, Tier.POSITIONS}
    # Mark them run at t=1000.
    for t in due0:
        sched._last_run[t] = 1000.0
    # 25s later: only positions (20s) and candidates (30s? no, 30>25) ... positions due.
    due1 = sched.due_tiers(MarketSession.PRIMARY, mono=1025.0)
    assert Tier.POSITIONS in due1  # 25 >= 20
    assert Tier.BROAD not in due1  # 25 < 300
    assert Tier.WATCHLIST not in due1  # 25 < 60


async def test_tick_runs_due_tiers_in_order() -> None:
    calls: list[Tier] = []

    class FakeFunnel:
        async def run_tier1(self):
            calls.append(Tier.BROAD)

        async def run_tier2(self):
            calls.append(Tier.WATCHLIST)

        async def run_tier3(self):
            calls.append(Tier.CANDIDATES)

        async def run_tier4(self):
            calls.append(Tier.POSITIONS)

    sched = SessionScheduler(clock=MarketClock(), schedule=TierSchedule(DEFAULT_SCHEDULE))
    now = datetime(2026, 7, 16, 10, 0, tzinfo=_ET)  # PRIMARY
    ran = await sched.tick(FakeFunnel(), now=now, mono=5000.0)
    # Ascending tier order so T1 feeds T2 within the tick.
    assert ran == [Tier.BROAD, Tier.WATCHLIST, Tier.CANDIDATES, Tier.POSITIONS]
    assert calls == ran


async def test_tick_failure_isolated_and_continues() -> None:
    class HalfBrokenFunnel:
        async def run_tier1(self):
            raise RuntimeError("provider down")

        async def run_tier2(self):
            pass

        async def run_tier3(self):
            pass

        async def run_tier4(self):
            pass

    sched = SessionScheduler(clock=MarketClock(), schedule=TierSchedule(DEFAULT_SCHEDULE))
    now = datetime(2026, 7, 16, 10, 0, tzinfo=_ET)
    ran = await sched.tick(HalfBrokenFunnel(), now=now, mono=1.0)
    # Tier 1 raised, but the rest still ran.
    assert Tier.BROAD not in ran
    assert {Tier.WATCHLIST, Tier.CANDIDATES, Tier.POSITIONS} <= set(ran)
