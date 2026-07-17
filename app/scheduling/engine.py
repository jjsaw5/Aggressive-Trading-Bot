"""Session-aware scheduler: drive each funnel tier at its own session cadence.

Rather than many fixed-interval jobs, a single control loop ticks frequently,
asks the clock which session we're in, and runs each tier whose cadence for that
session has elapsed. This adapts cadence as the market moves through the day and
gates tiers off when they shouldn't run (e.g. broad scan overnight). Under
budget pressure it sheds the low-priority tiers first, protecting position
monitoring.

Ships gated OFF (TIERING_ENABLED=false): the scheduler process keeps running the
simple periodic scan until you flip the funnel on.
"""

from __future__ import annotations

import asyncio
import time as _time

from app.config import settings
from app.events.bus import get_event_bus
from app.events.types import provider_failure
from app.logging_config import get_logger
from app.observability.metrics import get_metrics
from app.providers.ratelimit import get_limiter
from app.scheduling.clock import MarketClock, MarketSession
from app.scheduling.schedule import TierSchedule, load_schedule
from app.tiers.funnel import FunnelEngine, build_funnel_engine
from app.tiers.models import Tier

log = get_logger(__name__)


class SessionScheduler:
    def __init__(
        self,
        *,
        clock: MarketClock | None = None,
        schedule: TierSchedule | None = None,
        funnel_factory=build_funnel_engine,
        tick_seconds: int | None = None,
    ) -> None:
        self.clock = clock or MarketClock()
        self.schedule = schedule or load_schedule(settings.scheduling_config_path)
        self._funnel_factory = funnel_factory
        self.tick_seconds = tick_seconds or settings.session_tick_seconds
        self._last_run: dict[Tier, float] = {}
        self._running = False

    # --- Pure scheduling decisions (testable) ---
    def due_tiers(self, session: MarketSession, mono: float) -> list[Tier]:
        due: list[Tier] = []
        for tier in Tier:
            cad = self.schedule.cadence(session, tier)
            if cad is None:
                continue
            last = self._last_run.get(tier)
            if last is None or (mono - last) >= cad:
                due.append(tier)
        return sorted(due)  # ascending tier so T1 feeds T2 within a tick

    def _budget_pressure(self) -> bool:
        if not settings.api_budget_enabled:
            return False
        cap = settings.api_daily_budget
        budgets = get_limiter().stats().get("budgets", {})
        return any(v >= cap * 0.95 for v in budgets.values())

    def _allowed(self, tier: Tier) -> bool:
        # Shed low-priority tiers first when the API budget is nearly spent;
        # candidates and positions keep running.
        if tier in (Tier.BROAD, Tier.WATCHLIST) and self._budget_pressure():
            return False
        return True

    async def _run_tier(self, funnel: FunnelEngine, tier: Tier) -> None:
        if tier == Tier.BROAD:
            await funnel.run_tier1()
        elif tier == Tier.WATCHLIST:
            await funnel.run_tier2()
        elif tier == Tier.CANDIDATES:
            await funnel.run_tier3()
        elif tier == Tier.POSITIONS:
            await funnel.run_tier4()

    async def tick(self, funnel: FunnelEngine, *, now=None, mono: float | None = None) -> list[Tier]:
        session = self.clock.session(now)
        mono = mono if mono is not None else _time.monotonic()
        metrics = get_metrics()
        metrics.inc("scheduler.ticks")
        metrics.inc(f"scheduler.session.{session.value}")
        ran: list[Tier] = []
        for tier in self.due_tiers(session, mono):
            if not self._allowed(tier):
                metrics.inc("scheduler.throttled")
                continue
            try:
                await self._run_tier(funnel, tier)
                ran.append(tier)
                metrics.inc(f"scheduler.tier.{tier.name.lower()}.runs")
            except Exception as exc:  # noqa: BLE001 - a tier failure must not stop the loop
                log.error("tier_run_failed", tier=tier.name, error=str(exc))
                metrics.inc("scheduler.tier_failures")
                if settings.events_enabled:
                    await get_event_bus().publish(
                        provider_failure(f"tier:{tier.name}", error=str(exc))
                    )
            self._last_run[tier] = mono
        return ran

    async def run_forever(self) -> None:
        funnel = self._funnel_factory()
        self._running = True
        log.info("session_scheduler_started", tick_seconds=self.tick_seconds)
        while self._running:
            try:
                ran = await self.tick(funnel)
                if ran:
                    log.info(
                        "session_tick",
                        session=self.clock.session().value,
                        ran=[t.name for t in ran],
                    )
            except Exception as exc:  # noqa: BLE001
                log.error("session_tick_failed", error=str(exc))
            await asyncio.sleep(self.tick_seconds)

    def stop(self) -> None:
        self._running = False
