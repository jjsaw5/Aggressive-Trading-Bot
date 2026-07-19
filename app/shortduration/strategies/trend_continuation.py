"""1-5DTE trend continuation.

A daily trend (SMA20 vs SMA50 + distance, via the existing price-action analyzer)
that the intraday tape confirms (price on the trend side of VWAP), with a clear
invalidation at the moving average. Reuses `analyze_price_action` so the daily
trend definition stays consistent with the core scanner.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import Direction, DTECategory, ShortDurationStrategy
from app.engine.price_action import analyze_price_action
from app.shortduration.strategies.base import (
    SetupContext,
    StrategyDetection,
    clamp01,
    flow_confirms,
    regime_supports,
)


@dataclass(frozen=True)
class TrendContinuationConfig:
    min_daily_score: float = 0.55  # price-action strength floor
    require_intraday_alignment: bool = True  # price on the trend side of VWAP


class TrendContinuation:
    key = ShortDurationStrategy.TREND_CONTINUATION
    dte_category = DTECategory.SHORT_DTE

    def __init__(self, config: TrendContinuationConfig | None = None) -> None:
        self.cfg = config or TrendContinuationConfig()

    def detect(self, ctx: SetupContext) -> StrategyDetection | None:
        if ctx.daily is None:
            return None
        pa = analyze_price_action(ctx.daily)
        if pa.direction not in (Direction.BULLISH, Direction.BEARISH):
            return None
        if pa.score < self.cfg.min_daily_score:
            return None
        direction = pa.direction

        lv = ctx.levels
        if self.cfg.require_intraday_alignment and lv is not None and lv.above_vwap is not None:
            if direction == Direction.BULLISH and lv.above_vwap is False:
                return None
            if direction == Direction.BEARISH and lv.above_vwap is True:
                return None
        if not regime_supports(ctx.regime, direction):
            return None

        sma20 = pa.details.get("sma20")
        reasons = [f"Daily {direction.value} trend: {pa.rationale}"]
        score = 0.5 + clamp01((pa.score - self.cfg.min_daily_score) / 0.45) * 0.25
        if lv is not None and lv.above_vwap is not None:
            reasons.append(f"Intraday {'above' if lv.above_vwap else 'below'} VWAP — aligned.")
            score += 0.1
        fc = flow_confirms(ctx.flow, direction)
        if fc:
            reasons.append("Multi-session flow confirms.")
            score += 0.15
        if not ctx.regime.allow_new_trades:
            reasons.append("NOTE: regime currently blocks new trades (event/vol).")

        inval = (
            f"Daily close back below SMA20 ({sma20:g})."
            if direction == Direction.BULLISH and sma20 is not None
            else f"Daily close back above SMA20 ({sma20:g})."
            if sma20 is not None
            else "Daily close back through SMA20."
        )
        return StrategyDetection(
            strategy=self.key,
            dte_category=self.dte_category,
            direction=direction,
            setup_score=clamp01(score),
            entry_trigger="Enter on an intraday continuation in the trend direction.",
            invalidation=inval,
            reasons=reasons,
            targets=[],
        )
