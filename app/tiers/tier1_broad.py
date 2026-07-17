"""Tier 1 — broad-universe lightweight screen.

Cheap by design: quote + fundamentals + catalyst presence + a liquidity gate.
NO option chain, NO flow, NO IV, NO deep scoring — that is what makes sweeping
500-1500 symbols affordable. Output is the top candidates promoted to the Tier-2
watchlist. Runs at BROAD priority so it yields API budget to everything else.
"""

from __future__ import annotations

from app.engine.liquidity import gate_underlying
from app.engine.universe import UniverseConfig
from app.logging_config import get_logger
from app.providers.base import (
    CalendarProvider,
    FundamentalsProvider,
    MarketDataProvider,
)
from app.providers.ratelimit import Priority, use_priority
from app.tiers.concurrency import bounded_gather
from app.tiers.models import Tier1Result

log = get_logger(__name__)


def score_tier1(gap_pct: float, rel_volume: float, has_catalyst: bool) -> float:
    """Lightweight interest score in [0, 1]: gap magnitude, relative volume,
    and catalyst presence. Saturating so extremes don't dominate."""
    g = min(abs(gap_pct) / 5.0, 1.0)  # a 5% gap saturates
    v = min(max(rel_volume - 1.0, 0.0) / 3.0, 1.0)  # 4x relative volume saturates
    c = 1.0 if has_catalyst else 0.0
    return round(0.4 * g + 0.4 * v + 0.2 * c, 4)


class Tier1BroadScanner:
    def __init__(
        self,
        *,
        market: MarketDataProvider,
        fundamentals: FundamentalsProvider,
        calendar: CalendarProvider,
        universe: UniverseConfig | None = None,
        concurrency: int = 8,
    ) -> None:
        self.market = market
        self.fundamentals = fundamentals
        self.calendar = calendar
        self.universe = universe or UniverseConfig()
        self.concurrency = concurrency

    async def evaluate(self, symbol: str) -> Tier1Result:
        quote = await self.market.get_quote(symbol)
        fund = await self.fundamentals.get_fundamentals(symbol)

        gap_pct = (quote.change_pct or 0.0) * 100.0
        # Relative volume via dollar volume (avg_dollar_volume is what we have).
        rel_volume = 1.0
        if fund.avg_dollar_volume and quote.volume:
            rel_volume = (quote.price * quote.volume) / fund.avg_dollar_volume

        rejects = gate_underlying(fund, quote.price, self.universe)
        passed = not rejects

        has_catalyst = False
        if passed:  # only spend the calendar call on names that clear liquidity
            has_catalyst = bool(await self.calendar.get_catalysts(symbol))

        score = score_tier1(gap_pct, rel_volume, has_catalyst) if passed else 0.0
        return Tier1Result(
            symbol=symbol.upper(),
            passed=passed,
            score=score,
            gap_pct=round(gap_pct, 4),
            rel_volume=round(rel_volume, 3),
            has_catalyst=has_catalyst,
            reasons=[r.value for r in rejects],
        )

    async def run(self, symbols: list[str] | None = None) -> list[Tier1Result]:
        syms = symbols or self.universe.normalized_symbols()
        with use_priority(Priority.BROAD):
            results = await bounded_gather(
                [self.evaluate(s) for s in syms], limit=self.concurrency
            )
        out = [r for r in results if r is not None]
        out.sort(key=lambda r: r.score, reverse=True)
        log.info("tier1_complete", evaluated=len(syms), passed=sum(r.passed for r in out))
        return out
