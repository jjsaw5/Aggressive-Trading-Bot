"""0DTE VWAP trend continuation.

Bullish: price above VWAP with an up-sloping intraday structure whose pullbacks
have held VWAP, ideally with volume expanding on the push. Bearish is the mirror.
Uses the intraday bars for structure; VWAP from the computed levels.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.domain.enums import Direction, DTECategory, ShortDurationStrategy
from app.shortduration.strategies.base import (
    SetupContext,
    StrategyDetection,
    clamp01,
    flow_confirms,
    regime_supports,
)


@dataclass(frozen=True)
class VWAPContinuationConfig:
    lookback_bars: int = 20
    min_abs_slope_pct: float = 0.0002  # per-bar close slope as a fraction of price
    require_pullback_held_vwap: bool = True


class VWAPTrendContinuation:
    key = ShortDurationStrategy.VWAP_TREND_CONTINUATION
    dte_category = DTECategory.ZERO_DTE

    def __init__(self, config: VWAPContinuationConfig | None = None) -> None:
        self.cfg = config or VWAPContinuationConfig()

    def detect(self, ctx: SetupContext) -> StrategyDetection | None:
        lv = ctx.levels
        cfg = self.cfg
        if lv is None or lv.last is None or lv.vwap is None:
            return None
        window = ctx.bars_1m[-cfg.lookback_bars:]
        if len(window) < max(6, cfg.lookback_bars // 2):
            return None

        closes = np.asarray([b.close for b in window], dtype=float)
        x = np.arange(closes.size, dtype=float)
        slope = float(np.polyfit(x, closes, 1)[0])  # price change per bar
        slope_pct = slope / lv.last if lv.last else 0.0

        if lv.last > lv.vwap and slope_pct >= cfg.min_abs_slope_pct:
            direction = Direction.BULLISH
        elif lv.last < lv.vwap and slope_pct <= -cfg.min_abs_slope_pct:
            direction = Direction.BEARISH
        else:
            return None

        # Pullbacks held VWAP: over the window, price didn't close on the wrong
        # side of VWAP (a clean trend rides VWAP, it doesn't lose it).
        if cfg.require_pullback_held_vwap:
            if direction == Direction.BULLISH and float(closes.min()) < lv.vwap:
                return None
            if direction == Direction.BEARISH and float(closes.max()) > lv.vwap:
                return None

        if not regime_supports(ctx.regime, direction):
            return None

        # Volume expansion on continuation: last third vs first third of window.
        vols = np.asarray([b.volume for b in window], dtype=float)
        third = max(1, vols.size // 3)
        expanding = float(vols[-third:].mean()) > float(vols[:third].mean())

        reasons = [
            f"Price {'above' if direction == Direction.BULLISH else 'below'} VWAP "
            f"({lv.last:g} vs {lv.vwap:g}) with a {'rising' if slope > 0 else 'falling'} "
            f"{len(window)}-bar structure."
        ]
        score = 0.5 + clamp01(abs(slope_pct) / 0.002) * 0.2
        if expanding:
            reasons.append("Volume expanding on continuation.")
            score += 0.15
        else:
            reasons.append("Volume not yet expanding — weaker continuation.")
        fc = flow_confirms(ctx.flow, direction)
        if fc:
            reasons.append("Options flow confirms direction.")
            score += 0.15
        if not ctx.regime.allow_new_trades:
            reasons.append("NOTE: regime currently blocks new trades (event/vol).")

        return StrategyDetection(
            strategy=self.key,
            dte_category=self.dte_category,
            direction=direction,
            setup_score=clamp01(score),
            entry_trigger=f"Enter on a pullback to VWAP ({lv.vwap:g}) that holds.",
            invalidation=f"A close through VWAP ({lv.vwap:g}) against the trend.",
            reasons=reasons,
            targets=[],
        )
