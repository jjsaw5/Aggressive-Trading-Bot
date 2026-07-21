"""0DTE VWAP trend continuation — quality-graded.

Bullish: price above VWAP with an up-sloping intraday structure. Rather than a
single all-or-nothing "pullbacks never lost VWAP" gate, the continuation is graded
by the VWAP continuation-quality model (six named sub-scores; see
`vwap_quality.py`) and must clear a minimum composite. That lets a brief, cleanly
reclaimed VWAP loss through on its `controlled_reclaim` merit while a genuine
whipsaw still fails on weak hold + reclaim. Bearish is the mirror.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.config import get_settings
from app.domain.enums import Direction, DTECategory, ShortDurationStrategy
from app.shortduration.strategies.base import (
    SetupContext,
    StrategyDetection,
    clamp01,
    flow_confirms,
    regime_supports,
)
from app.shortduration.strategies.vwap_quality import compute_vwap_quality


@dataclass(frozen=True)
class VWAPContinuationConfig:
    lookback_bars: int = 20
    min_abs_slope_pct: float = 0.0002  # per-bar close slope as a fraction of price
    min_quality: float = 0.45          # minimum composite continuation-quality to fire

    @classmethod
    def from_settings(cls) -> VWAPContinuationConfig:
        s = get_settings()
        return cls(
            lookback_bars=s.vwap_lookback_bars,
            min_abs_slope_pct=s.vwap_min_abs_slope_pct,
            min_quality=s.vwap_min_quality,
        )


class VWAPTrendContinuation:
    key = ShortDurationStrategy.VWAP_TREND_CONTINUATION
    dte_category = DTECategory.ZERO_DTE

    def __init__(self, config: VWAPContinuationConfig | None = None) -> None:
        self.cfg = config or VWAPContinuationConfig.from_settings()

    def detect(self, ctx: SetupContext) -> StrategyDetection | None:
        lv = ctx.levels
        cfg = self.cfg
        if lv is None or lv.last is None or lv.vwap is None:
            return None
        window = ctx.bars_1m[-cfg.lookback_bars:]
        if len(window) < max(6, cfg.lookback_bars // 2):
            return None

        closes = np.asarray([b.close for b in window], dtype=float)
        highs = np.asarray([b.high for b in window], dtype=float)
        lows = np.asarray([b.low for b in window], dtype=float)
        vols = np.asarray([b.volume for b in window], dtype=float)
        x = np.arange(closes.size, dtype=float)
        slope = float(np.polyfit(x, closes, 1)[0])  # price change per bar
        slope_pct = slope / lv.last if lv.last else 0.0

        if lv.last > lv.vwap and slope_pct >= cfg.min_abs_slope_pct:
            direction = Direction.BULLISH
        elif lv.last < lv.vwap and slope_pct <= -cfg.min_abs_slope_pct:
            direction = Direction.BEARISH
        else:
            return None

        if not regime_supports(ctx.regime, direction):
            return None

        quality = compute_vwap_quality(
            closes, highs, lows, vols,
            vwap=lv.vwap, last=lv.last, direction=direction, slope_pct=slope_pct,
        )
        # Graded gate: the continuation must be good enough overall. A whipsaw fails
        # here (low vwap_hold + low controlled_reclaim); a clean reclaim survives.
        if quality.overall < cfg.min_quality:
            return None

        reasons = [
            f"Price {'above' if direction == Direction.BULLISH else 'below'} VWAP "
            f"({lv.last:g} vs {lv.vwap:g}) with a {'rising' if slope > 0 else 'falling'} "
            f"{len(window)}-bar structure; continuation quality {quality.overall:.2f}.",
            f"Sub-scores — continuation {quality.continuation:.2f}, structure "
            f"{quality.structure:.2f}, VWAP-hold {quality.vwap_hold:.2f}, pullback "
            f"{quality.pullback:.2f}, volume {quality.volume:.2f}, "
            f"reclaim {quality.controlled_reclaim:.2f}.",
        ]
        reasons.extend(quality.notes)
        # Score is anchored on the graded quality, nudged by flow confirmation.
        score = 0.4 + quality.overall * 0.45
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
            invalidation=f"A decisive close through VWAP ({lv.vwap:g}) against the trend.",
            reasons=reasons,
            targets=[],
            metadata=quality.as_metadata(),
        )
