"""Funnel orchestrator: run one pass of Tier 1 → 2 → 3 with promotion/demotion,
plus Tier 4 position monitoring, persisting membership and publishing events.

Promotion is "top-N by score into the next tier"; demotion is implicit — a
symbol not re-promoted simply isn't in the tier's new membership (which is
replaced atomically each pass). Tier 4 runs every pass regardless, because open
positions are always the priority.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.config import settings
from app.domain.candidates import TradeCandidate
from app.engine.universe import UniverseConfig
from app.events.bus import get_event_bus
from app.events.types import Event, EventType
from app.logging_config import get_logger
from app.providers import registry
from app.tiers.models import PositionRisk, Tier, TierMember
from app.tiers.store import TierStore
from app.tiers.tier1_broad import Tier1BroadScanner
from app.tiers.tier2_watchlist import Tier2WatchlistScanner
from app.tiers.tier3_candidates import Tier3CandidateEvaluator
from app.tiers.tier4_positions import Tier4PositionMonitor

log = get_logger(__name__)


class FunnelReport(BaseModel):
    tier1_evaluated: int = 0
    tier1_passed: int = 0
    watchlist: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    tier3_actionable: int = 0
    positions_monitored: int = 0
    position_risks: list[PositionRisk] = Field(default_factory=list)
    events_published: int = 0


class FunnelEngine:
    def __init__(
        self,
        *,
        market,
        fundamentals,
        calendar,
        flow,
        chain,
        iv_history=None,
        universe: UniverseConfig | None = None,
        store: TierStore | None = None,
        tier3: Tier3CandidateEvaluator | None = None,
        watchlist_max: int | None = None,
        candidates_max: int | None = None,
        concurrency: int | None = None,
    ) -> None:
        c = concurrency or settings.tier_concurrency
        self.universe = universe or UniverseConfig()
        self.store = store or TierStore()
        self.watchlist_max = watchlist_max or settings.tier_watchlist_max
        self.candidates_max = candidates_max or settings.tier_candidates_max
        self.tier1 = Tier1BroadScanner(
            market=market, fundamentals=fundamentals, calendar=calendar,
            universe=self.universe, concurrency=c,
        )
        self.tier2 = Tier2WatchlistScanner(
            market=market, flow=flow, chain=chain, iv_history=iv_history, concurrency=c,
        )
        self.tier3 = tier3 or Tier3CandidateEvaluator()
        self.tier4 = Tier4PositionMonitor(chain=chain, concurrency=c)

    async def _publish_position_events(self, risks: list[PositionRisk]) -> int:
        if not settings.events_enabled or not risks:
            return 0
        bus = get_event_bus()
        n = 0
        for r in risks:
            await bus.publish(
                Event(
                    type=EventType.POSITION_UPDATED,
                    symbol=r.symbol,
                    payload={"trade_id": r.trade_id, "pnl_usd": r.pnl_usd, "action": r.action},
                    source="tier4",
                )
            )
            n += 1
            if r.action != "hold":
                await bus.publish(
                    Event(
                        type=EventType.RISK_THRESHOLD_REACHED,
                        symbol=r.symbol,
                        payload={"trade_id": r.trade_id, "action": r.action, "note": r.note},
                        source="tier4",
                    )
                )
                n += 1
        return n

    # --- Per-tier steps (each reads/writes tier membership via the store, so
    #     the session scheduler can run them at independent cadences) ---
    async def run_tier1(self, symbols: list[str] | None = None) -> list[str]:
        """Tier 1 sweep -> promote top passers to the watchlist. Returns watchlist."""
        t1 = await self.tier1.run(symbols)
        passed = [r for r in t1 if r.passed]
        await self.store.replace(
            Tier.BROAD,
            [
                TierMember(
                    symbol=r.symbol, tier=Tier.BROAD, score=r.score,
                    reason=f"gap={r.gap_pct}% relvol={r.rel_volume} cat={r.has_catalyst}",
                    metrics={"gap_pct": r.gap_pct, "rel_volume": r.rel_volume},
                )
                for r in passed[:200]
            ],
        )
        watch = [r.symbol for r in passed[: self.watchlist_max]]
        await self.store.replace(
            Tier.WATCHLIST,
            [TierMember(symbol=r.symbol, tier=Tier.WATCHLIST, score=r.score)
             for r in passed[: self.watchlist_max]],
        )
        self._last_tier1_count = len(t1)
        self._last_tier1_passed = len(passed)
        return watch

    async def run_tier2(self) -> list[str]:
        """Score the current watchlist -> promote top to candidates. Returns candidates."""
        watch = await self.store.symbols(Tier.WATCHLIST)
        if not watch:
            return []
        t2 = await self.tier2.run(watch)
        await self.store.replace(
            Tier.WATCHLIST,
            [
                TierMember(
                    symbol=r.symbol, tier=Tier.WATCHLIST, score=r.score,
                    reason=f"dir={r.direction.value if r.direction else 'none'}",
                    metrics={"flow": r.flow_score, "price": r.price_score, "vol": r.vol_score},
                )
                for r in t2
            ],
        )
        cand = [r.symbol for r in t2[: self.candidates_max]]
        await self.store.replace(
            Tier.CANDIDATES,
            [TierMember(symbol=r.symbol, tier=Tier.CANDIDATES, score=r.score)
             for r in t2[: self.candidates_max]],
        )
        return cand

    async def run_tier3(self) -> list[TradeCandidate]:
        """Deep-evaluate the current candidate set."""
        cand = await self.store.symbols(Tier.CANDIDATES)
        return await self.tier3.run(cand)

    async def run_tier4(self) -> list[PositionRisk]:
        """Monitor open positions and publish risk events."""
        import asyncio

        from app.db import repository

        trades = await asyncio.to_thread(repository.list_paper_trades, 200)
        risks = await self.tier4.run(trades)
        await self.store.replace(
            Tier.POSITIONS,
            [TierMember(symbol=r.symbol, tier=Tier.POSITIONS, score=r.pnl_pct, reason=r.action)
             for r in risks],
        )
        await self._publish_position_events(risks)
        return risks

    async def run_once(self, symbols: list[str] | None = None) -> FunnelReport:
        """One full cascade T1->T2->T3 + T4. Used on demand (API) and in tests;
        the scheduler runs the per-tier steps at independent cadences instead."""
        self._last_tier1_count = 0
        self._last_tier1_passed = 0
        watch = await self.run_tier1(symbols)
        cand = await self.run_tier2()
        t3 = await self.run_tier3()
        risks = await self.run_tier4()
        report = FunnelReport(
            tier1_evaluated=self._last_tier1_count,
            tier1_passed=self._last_tier1_passed,
            watchlist=watch,
            candidates=cand,
            tier3_actionable=sum(c.is_actionable for c in t3),
            positions_monitored=len(risks),
            position_risks=risks,
            events_published=sum(1 for _ in risks),  # approximate; events already published
        )
        log.info(
            "funnel_pass",
            t1=report.tier1_evaluated, watch=len(report.watchlist),
            cands=len(report.candidates), actionable=report.tier3_actionable,
            positions=report.positions_monitored,
        )
        return report


def build_funnel_engine(universe: UniverseConfig | None = None) -> FunnelEngine:
    """Wire a funnel from the configured provider registry."""
    return FunnelEngine(
        market=registry.market_data_provider(),
        fundamentals=registry.fundamentals_provider(),
        calendar=registry.calendar_provider(),
        flow=registry.options_flow_provider(),
        chain=registry.options_chain_provider(),
        iv_history=registry.iv_history_provider(),
        universe=universe,
    )
