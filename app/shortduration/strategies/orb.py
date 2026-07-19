"""0DTE Opening-Range Breakout.

Bullish: price closes beyond the opening-range high (not just a wick), holds
above VWAP, on expanding relative volume, with the regime not contradicting.
Bearish is the mirror. Configurable OR window (the levels carry it) and
thresholds. Requires the OR to be established — no premature reads.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import Direction, DTECategory, ShortDurationStrategy
from app.shortduration.strategies.base import (
    SetupContext,
    StrategyDetection,
    clamp01,
    flow_confirms,
    regime_supports,
)


@dataclass(frozen=True)
class ORBConfig:
    min_rel_volume: float = 1.3
    min_break_pct: float = 0.0005  # 0.05% beyond the range to count as a break
    require_vwap_alignment: bool = True


class OpeningRangeBreakout:
    key = ShortDurationStrategy.OPENING_RANGE_BREAKOUT
    dte_category = DTECategory.ZERO_DTE

    def __init__(self, config: ORBConfig | None = None) -> None:
        self.cfg = config or ORBConfig()

    def detect(self, ctx: SetupContext) -> StrategyDetection | None:
        lv = ctx.levels
        if lv is None or lv.last is None or lv.opening_range_high is None or lv.opening_range_low is None:
            return None
        cfg = self.cfg
        orh, orl, last = lv.opening_range_high, lv.opening_range_low, lv.last

        if last >= orh * (1 + cfg.min_break_pct):
            direction, level = Direction.BULLISH, orh
        elif last <= orl * (1 - cfg.min_break_pct):
            direction, level = Direction.BEARISH, orl
        else:
            return None

        # Confirmation, not a wick: the last completed bar must CLOSE beyond it.
        if ctx.bars_1m:
            last_close = ctx.bars_1m[-1].close
            if direction == Direction.BULLISH and last_close < orh:
                return None
            if direction == Direction.BEARISH and last_close > orl:
                return None

        if cfg.require_vwap_alignment and lv.vwap is not None:
            if direction == Direction.BULLISH and last < lv.vwap:
                return None
            if direction == Direction.BEARISH and last > lv.vwap:
                return None

        relvol = lv.relative_volume
        if relvol is not None and relvol < cfg.min_rel_volume:
            return None
        if not regime_supports(ctx.regime, direction):
            return None

        reasons = [
            f"Broke opening range {'high' if direction == Direction.BULLISH else 'low'} "
            f"({level:g}); last {last:g}."
        ]
        score = 0.5
        if relvol is not None:
            reasons.append(f"Relative volume {relvol:g}x.")
            score += clamp01((relvol - cfg.min_rel_volume) / 2) * 0.2
        if lv.vwap is not None:
            reasons.append(f"{'Above' if last > lv.vwap else 'Below'} VWAP {lv.vwap:g}.")
            score += 0.15
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
            entry_trigger=(
                f"Enter on hold above {level:g} (OR {'high' if direction == Direction.BULLISH else 'low'})."
                if direction == Direction.BULLISH
                else f"Enter on hold below {level:g} (OR low)."
            ),
            invalidation=f"Back inside the opening range (through {level:g}) / lose VWAP.",
            reasons=reasons,
            targets=[],
        )
