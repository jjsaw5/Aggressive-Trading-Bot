"""1-5DTE catalyst continuation.

A catalyst (recent material news for the name, or a scheduled event) PLUS
multi-session price continuation in the catalyst's direction, confirmed by
volume. The headline alone never triggers — the price/volume follow-through is
mandatory, per the module spec ("news may create a candidate; news alone must
not create an approved trade").
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import Direction, DTECategory, ShortDurationStrategy
from app.shortduration.strategies.base import (
    SetupContext,
    StrategyDetection,
    clamp01,
    regime_supports,
)


@dataclass(frozen=True)
class CatalystContinuationConfig:
    sessions: int = 3
    min_move_pct: float = 0.02  # net move over the window to count as continuation
    news_lookback_hours: float = 48.0
    require_volume_confirm: bool = True


class CatalystContinuation:
    key = ShortDurationStrategy.CATALYST_CONTINUATION
    dte_category = DTECategory.SHORT_DTE

    def __init__(self, config: CatalystContinuationConfig | None = None) -> None:
        self.cfg = config or CatalystContinuationConfig()

    def _has_catalyst(self, ctx: SetupContext) -> str | None:
        cutoff = ctx.now.timestamp() - self.cfg.news_lookback_hours * 3600
        fresh = [
            n for n in ctx.news
            if n.source_ts is not None and n.source_ts.timestamp() >= cutoff
        ]
        if fresh:
            return f"{len(fresh)} recent headline(s), e.g. “{fresh[0].headline[:80]}”"
        if ctx.catalysts:
            c = ctx.catalysts[0]
            return f"scheduled {c.event_type} on {c.event_date}"
        return None

    def detect(self, ctx: SetupContext) -> StrategyDetection | None:
        cfg = self.cfg
        catalyst = self._has_catalyst(ctx)
        if catalyst is None:
            return None
        if ctx.daily is None:
            return None
        closes = ctx.daily.closes
        if len(closes) < cfg.sessions + 1:
            return None

        ref = closes[-1 - cfg.sessions]
        move = (closes[-1] - ref) / ref if ref else 0.0
        if move >= cfg.min_move_pct:
            direction = Direction.BULLISH
        elif move <= -cfg.min_move_pct:
            direction = Direction.BEARISH
        else:
            return None  # catalyst without price continuation -> not actionable
        if not regime_supports(ctx.regime, direction):
            return None

        vols = [c.volume for c in ctx.daily.candles]
        vol_ok = True
        if cfg.require_volume_confirm and len(vols) >= 20:
            recent = sum(vols[-cfg.sessions:]) / cfg.sessions
            base = sum(vols[-20:]) / 20
            vol_ok = recent >= base
            if not vol_ok:
                return None

        reasons = [
            f"Catalyst: {catalyst}.",
            f"{cfg.sessions}-session move {move * 100:+.1f}% — continuation confirms direction.",
        ]
        score = 0.5 + clamp01((abs(move) - cfg.min_move_pct) / 0.06) * 0.25
        if vol_ok and len(vols) >= 20:
            reasons.append("Volume confirms the move.")
            score += 0.15
        if not ctx.regime.allow_new_trades:
            reasons.append("NOTE: regime currently blocks new trades (event/vol).")

        return StrategyDetection(
            strategy=self.key,
            dte_category=self.dte_category,
            direction=direction,
            setup_score=clamp01(score),
            entry_trigger="Enter on a continuation day in the catalyst direction.",
            invalidation=f"A full retrace of the {cfg.sessions}-session catalyst move.",
            reasons=reasons,
            targets=[],
        )
