"""Tier 2 — active-watchlist medium evaluation.

Deeper than Tier 1, cheaper than Tier 3: adds options flow, price/trend action,
and IV context (a single volatility call), but still NO full option chain. It
resolves a directional thesis and a composite score, promoting the strongest
names to Tier 3. Runs at WATCHLIST priority.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.enums import Direction
from app.domain.signals import SignalBundle
from app.engine.flow import analyze_flow
from app.engine.iv_context import build_iv_context
from app.engine.price_action import analyze_price_action
from app.engine.scoring import ScoreWeights, composite_score, resolve_direction
from app.engine.volatility import analyze_volatility
from app.logging_config import get_logger
from app.providers.base import (
    IVHistoryProvider,
    MarketDataProvider,
    OptionsChainProvider,
    OptionsFlowProvider,
)
from app.providers.ratelimit import Priority, use_priority
from app.tiers.concurrency import bounded_gather
from app.tiers.models import Tier2Result

log = get_logger(__name__)


class Tier2WatchlistScanner:
    def __init__(
        self,
        *,
        market: MarketDataProvider,
        flow: OptionsFlowProvider,
        chain: OptionsChainProvider,
        iv_history: IVHistoryProvider | None = None,
        weights: ScoreWeights | None = None,
        concurrency: int = 8,
    ) -> None:
        self.market = market
        self.flow = flow
        self.chain = chain
        self.iv_history = iv_history
        self.weights = weights or ScoreWeights()
        self.concurrency = concurrency

    async def evaluate(self, symbol: str) -> Tier2Result:
        now = datetime.now(UTC)
        history = await self.market.get_price_history(symbol, lookback_days=252)
        flow_alerts = await self.flow.get_flow_alerts(symbol=symbol, unusual_only=True)
        current = await self.chain.get_iv_context(symbol)

        iv_hist = None
        if self.iv_history is not None:
            try:
                iv_hist = await self.iv_history.get_iv_history(symbol, lookback_days=365)
            except Exception as exc:  # noqa: BLE001
                log.warning("tier2_iv_history_failed", symbol=symbol, error=str(exc))

        iv = build_iv_context(
            symbol,
            current.iv30,
            now,
            iv_history=iv_hist,
            price_history=history,
            term_structure_slope=current.term_structure_slope,
        )

        flow_sig = analyze_flow(symbol, flow_alerts)
        price_sig = analyze_price_action(history)
        direction = resolve_direction(flow_sig, price_sig)
        vol_sig = analyze_volatility(iv, direction or Direction.NEUTRAL)

        bundle = SignalBundle(symbol=symbol, scores=[flow_sig, price_sig, vol_sig])
        score = composite_score(bundle, self.weights)
        return Tier2Result(
            symbol=symbol.upper(),
            score=score,
            direction=direction,
            flow_score=round(flow_sig.score, 4),
            price_score=round(price_sig.score, 4),
            vol_score=round(vol_sig.score, 4),
        )

    async def run(self, symbols: list[str]) -> list[Tier2Result]:
        with use_priority(Priority.WATCHLIST):
            results = await bounded_gather(
                [self.evaluate(s) for s in symbols], limit=self.concurrency
            )
        out = [r for r in results if r is not None]
        out.sort(key=lambda r: r.score, reverse=True)
        log.info("tier2_complete", evaluated=len(symbols))
        return out
