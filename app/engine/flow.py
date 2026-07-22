"""Options-flow analyzer.

Answers: *is the options flow meaningful or likely noise?* and *what direction
does it imply?* Produces a `SignalScore` from a list of `FlowAlert`s.

Heuristics (deliberately conservative — flow is evidence, not proof):
  * Meaningfulness scales with aggregate premium, sweep participation, and
    opening (vs closing) prints — a few large opening sweeps beat many tiny
    prints.
  * Direction is the premium-weighted net sentiment.
  * Conflicting two-sided flow reduces confidence.
"""

from __future__ import annotations

from app.domain.enums import Direction
from app.domain.options import FlowAlert
from app.domain.signals import SignalScore
from app.engine.flow_quality import proprietary_flow_quality

# Premium (USD) at which flow is considered clearly "significant" for a
# mega-cap. Below this it is scored proportionally.
_SIGNIFICANT_PREMIUM = 1_000_000.0


def analyze_flow(symbol: str, alerts: list[FlowAlert]) -> SignalScore:
    relevant = [a for a in alerts if a.symbol.upper() == symbol.upper()]
    if not relevant:
        return SignalScore(
            name="options_flow",
            score=0.0,
            direction=Direction.NEUTRAL,
            confidence=0.2,
            rationale="No qualifying flow prints.",
            details={"alert_count": 0, "flow_quality_proprietary": None},
        )

    total_premium = sum(a.premium or 0.0 for a in relevant)
    sweep_premium = sum((a.premium or 0.0) for a in relevant if a.is_sweep)
    opening_premium = sum((a.premium or 0.0) for a in relevant if a.is_opening)

    # Premium-weighted net sentiment in [-1, 1].
    weighted = sum((a.premium or 0.0) * (a.sentiment or 0.0) for a in relevant)
    net_sentiment = weighted / total_premium if total_premium > 0 else 0.0

    # Bull vs bear premium split -> agreement (1 - two-sidedness).
    bull_prem = sum((a.premium or 0.0) for a in relevant if (a.sentiment or 0) > 0)
    bear_prem = sum((a.premium or 0.0) for a in relevant if (a.sentiment or 0) < 0)
    dominant = max(bull_prem, bear_prem)
    agreement = dominant / total_premium if total_premium > 0 else 0.0

    magnitude = min(1.0, total_premium / _SIGNIFICANT_PREMIUM)
    sweep_ratio = (sweep_premium / total_premium) if total_premium > 0 else 0.0
    opening_ratio = (opening_premium / total_premium) if total_premium > 0 else 0.0

    # Composite: size dominates, aggression (sweeps) and opening add conviction.
    score = (
        0.55 * magnitude
        + 0.20 * sweep_ratio
        + 0.15 * opening_ratio
        + 0.10 * abs(net_sentiment)
    )
    score = round(min(1.0, score) * agreement, 4)

    if net_sentiment > 0.15:
        direction = Direction.BULLISH
    elif net_sentiment < -0.15:
        direction = Direction.BEARISH
    else:
        direction = Direction.NEUTRAL

    return SignalScore(
        name="options_flow",
        score=score,
        direction=direction,
        confidence=round(agreement, 3),
        rationale=(
            f"{len(relevant)} prints, ${total_premium:,.0f} premium, "
            f"{sweep_ratio:.0%} sweeps, net sentiment {net_sentiment:+.2f}, "
            f"agreement {agreement:.0%}."
        ),
        details={
            "alert_count": len(relevant),
            "total_premium": round(total_premium, 2),
            "net_sentiment": round(net_sentiment, 3),
            "sweep_ratio": round(sweep_ratio, 3),
            "opening_ratio": round(opening_ratio, 3),
            "agreement": round(agreement, 3),
            # Shadow-only: recorded for the ledger, never fed into the score.
            "flow_quality_proprietary": proprietary_flow_quality(relevant),
        },
    )
