"""0DTE Opening-Range Breakout — adaptive.

Bullish: price breaks beyond the opening-range high by an *adaptive* buffer that
scales with the session's own volatility (the opening-range width), holds above
VWAP, on expanding relative volume, with the regime not contradicting. Bearish
is the mirror.

Three refinements over a flat threshold:

- **Adaptive buffer.** A wider, more volatile opening range needs more room before
  a poke counts as a break; a tight range needs less. The buffer is a fraction of
  the OR width, floored at a minimum fraction of price so a tiny range still needs
  a real move.
- **Anti-chase.** If price has already run far past the level (measured in OR
  widths), the entry is a chase — poor reward-to-risk, paying up into the move — so
  it is rejected, and the score is penalized as extension grows toward that cap.
- **Confirmation modes.** ``close`` (the last completed bar must close beyond the
  level — default), ``immediate`` (price beyond the adaptive buffer is enough), or
  ``retest`` (broke earlier, pulled back to the level, and resumed) — a stricter,
  higher-quality entry. Missing bar history is never treated as confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings
from app.domain.enums import Direction, DTECategory, ShortDurationStrategy
from app.shortduration.strategies.base import (
    SetupContext,
    StrategyDetection,
    clamp01,
    flow_confirms,
    regime_supports,
)

_MODES = ("close", "immediate", "retest")


@dataclass(frozen=True)
class ORBConfig:
    min_rel_volume: float = 1.3
    min_break_pct: float = 0.0005          # floor buffer as a fraction of price
    buffer_pct_of_range: float = 0.10      # adaptive buffer = this fraction of OR width
    max_extension_pct_of_range: float = 1.0  # anti-chase: reject past this many OR widths
    confirmation_mode: str = "close"       # "close" | "immediate" | "retest"
    retest_band_pct_of_range: float = 0.25  # retest pullback must reach within this of the level
    require_vwap_alignment: bool = True

    @classmethod
    def from_settings(cls) -> ORBConfig:
        s = get_settings()
        mode = s.orb_confirmation_mode if s.orb_confirmation_mode in _MODES else "close"
        return cls(
            min_rel_volume=s.orb_min_rel_volume,
            min_break_pct=s.orb_min_break_pct,
            buffer_pct_of_range=s.orb_buffer_pct_of_range,
            max_extension_pct_of_range=s.orb_max_extension_pct_of_range,
            confirmation_mode=mode,
            retest_band_pct_of_range=s.orb_retest_band_pct_of_range,
            require_vwap_alignment=s.orb_require_vwap_alignment,
        )


class OpeningRangeBreakout:
    key = ShortDurationStrategy.OPENING_RANGE_BREAKOUT
    dte_category = DTECategory.ZERO_DTE

    def __init__(self, config: ORBConfig | None = None) -> None:
        self.cfg = config or ORBConfig.from_settings()

    def _confirmed(
        self, ctx: SetupContext, direction: Direction, level: float, band: float
    ) -> bool | None:
        """Is the break confirmed under the configured mode? None when the inputs
        the mode needs are absent (never a silent pass)."""
        mode = self.cfg.confirmation_mode
        if mode == "immediate":
            return True  # price is already past the adaptive buffer (checked by caller)
        if not ctx.bars_1m:
            return None  # close/retest both need bar history
        last_close = ctx.bars_1m[-1].close
        closes_beyond = last_close >= level if direction == Direction.BULLISH else last_close <= level
        if mode == "close":
            return closes_beyond
        # retest: an earlier bar broke the level, a LATER bar (but not the current
        # one) pulled back to within `band` of it, and the current bar has resumed
        # beyond it. Three distinct roles -> at least three bars.
        bars = ctx.bars_1m
        if not closes_beyond or len(bars) < 3:
            return False
        broke_i = None
        for i, b in enumerate(bars[:-1]):  # the break must precede the current bar
            beyond = b.high >= level if direction == Direction.BULLISH else b.low <= level
            if beyond:
                broke_i = i
                break
        if broke_i is None:
            return False
        for b in bars[broke_i + 1 : -1]:  # a middle bar returns to the level
            near = (
                b.low <= level + band if direction == Direction.BULLISH
                else b.high >= level - band
            )
            if near:
                return True
        return False

    def detect(self, ctx: SetupContext) -> StrategyDetection | None:
        lv = ctx.levels
        if lv is None or lv.last is None or lv.opening_range_high is None or lv.opening_range_low is None:
            return None
        cfg = self.cfg
        orh, orl, last = lv.opening_range_high, lv.opening_range_low, lv.last
        or_width = max(orh - orl, 0.0)

        # Adaptive buffer: a fraction of the OR width, floored at a fraction of price.
        buffer = max(last * cfg.min_break_pct, cfg.buffer_pct_of_range * or_width)

        if last >= orh + buffer:
            direction, level = Direction.BULLISH, orh
        elif last <= orl - buffer:
            direction, level = Direction.BEARISH, orl
        else:
            return None

        # Anti-chase: how far past the level are we, in OR widths?
        extension = (last - orh) if direction == Direction.BULLISH else (orl - last)
        ext_ratio = (extension / or_width) if or_width > 0 else 0.0
        if or_width > 0 and ext_ratio > cfg.max_extension_pct_of_range:
            return None  # already extended — this is a chase, not a breakout entry

        band = cfg.retest_band_pct_of_range * or_width
        confirmed = self._confirmed(ctx, direction, level, band)
        if not confirmed:  # False or None (missing data) both block
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

        side = "high" if direction == Direction.BULLISH else "low"
        reasons = [
            f"Broke opening range {side} ({level:g}) by {buffer:g} "
            f"(adaptive {cfg.buffer_pct_of_range:.0%} of {or_width:g} range); last {last:g}."
        ]
        score = 0.5
        # A clean, un-extended break scores better than one that has already run.
        if or_width > 0:
            score += (1.0 - clamp01(ext_ratio / max(cfg.max_extension_pct_of_range, 1e-9))) * 0.1
            reasons.append(f"Extension {ext_ratio:.2f}x range past the level.")
        if cfg.confirmation_mode == "retest":
            reasons.append("Confirmed on a retest-and-hold of the level.")
            score += 0.1
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
                f"Enter on hold {'above' if direction == Direction.BULLISH else 'below'} "
                f"{level:g} (OR {side}); avoid chasing past {ext_ratio:.2f}x range extension."
            ),
            invalidation=f"Back inside the opening range (through {level:g}) / lose VWAP.",
            reasons=reasons,
            targets=[],
            metadata={
                "confirmation_mode": cfg.confirmation_mode,
                "or_width": round(or_width, 4),
                "breakout_buffer": round(buffer, 4),
                "extension_ratio": round(ext_ratio, 4),
                "level": round(level, 4),
            },
        )
