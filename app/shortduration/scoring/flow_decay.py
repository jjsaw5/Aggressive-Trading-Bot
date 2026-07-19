"""Options-flow decay + analysis for short-duration scoring.

Short-duration flow is time-sensitive: a sweep from 40 minutes ago is context,
not a signal. Each print is weighted by age (configurable buckets), and the
aggregate is judged on more than the provider's bull/bear label — opening vs
closing, opposing flow, and repeated strikes/expirations all shape confidence.
The provider's sentiment label is an input, never the final truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel

from app.domain.enums import Direction
from app.domain.options import FlowAlert


@dataclass(frozen=True)
class DecayConfig:
    # (max_age_seconds, weight). First bucket that fits wins; older => context.
    buckets: tuple[tuple[float, float], ...] = (
        (120, 1.0),      # 0-2 min: full
        (300, 0.8),      # 2-5 min: high
        (900, 0.5),      # 5-15 min: reduced
        (1800, 0.25),    # 15-30 min: low
    )
    context_weight: float = 0.1  # > 30 min


def decay_weight(age_seconds: float, cfg: DecayConfig | None = None) -> float:
    cfg = cfg or DecayConfig()
    for max_age, w in cfg.buckets:
        if age_seconds <= max_age:
            return w
    return cfg.context_weight


class FlowAnalysis(BaseModel):
    decayed_sentiment: float | None = None  # [-1,1], age-weighted
    confidence: float = 0.0  # [0,1] aligned, recent flow strength
    opening_fraction: float | None = None  # share of opening prints
    opposing_present: bool = False
    repeated_strikes: bool = False
    prints: int = 0
    explanation: str = ""


def analyze_flow(
    flow: list[FlowAlert], now: datetime, direction: Direction | None = None,
    cfg: DecayConfig | None = None,
) -> FlowAnalysis:
    """Age-weight prints and summarize direction/quality. `direction` (the setup
    direction) is used only to detect OPPOSING flow, not to cherry-pick."""
    if not flow:
        return FlowAnalysis(explanation="No flow available.")
    cfg = cfg or DecayConfig()
    num = den = 0.0
    max_weight = 0.0
    opening = openings = 0
    strikes: dict[float, int] = {}
    opposing = False
    for f in flow:
        age = max(0.0, (now - f.ts).total_seconds())
        w = decay_weight(age, cfg)
        max_weight = max(max_weight, w)
        if f.sentiment is not None:
            num += f.sentiment * w
            den += w
        if f.is_opening is not None:
            opening += 1
            openings += 1 if f.is_opening else 0
        if f.strike is not None:
            strikes[f.strike] = strikes.get(f.strike, 0) + 1
        if direction is not None and f.sentiment is not None:
            if direction == Direction.BULLISH and f.sentiment < -0.3:
                opposing = True
            if direction == Direction.BEARISH and f.sentiment > 0.3:
                opposing = True

    decayed = round(num / den, 3) if den > 0 else None
    repeated = any(c >= 2 for c in strikes.values())
    # Confidence: magnitude of decayed sentiment, scaled by how RECENT the flow
    # is (stale prints are weaker signals) and tempered by opposing flow.
    conf = 0.0
    if decayed is not None:
        conf = min(1.0, abs(decayed)) * max_weight
        if opposing:
            conf *= 0.5
    open_frac = round(openings / opening, 2) if opening else None
    parts = []
    if decayed is not None:
        parts.append(f"age-weighted sentiment {decayed:+.2f}")
    if open_frac is not None:
        parts.append(f"{int(open_frac * 100)}% opening")
    if repeated:
        parts.append("repeated-strike activity")
    if opposing:
        parts.append("opposing flow present")
    return FlowAnalysis(
        decayed_sentiment=decayed,
        confidence=round(conf, 3),
        opening_fraction=open_frac,
        opposing_present=opposing,
        repeated_strikes=repeated,
        prints=len(flow),
        explanation="; ".join(parts) or "flow present",
    )
