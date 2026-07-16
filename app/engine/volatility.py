"""Implied-volatility analyzer.

Answers: *is implied volatility favorable?* The answer is direction-aware:

  * For LONG-premium directional/vol-long theses (buying options), we prefer
    LOW-to-moderate IV rank (cheaper premium, room to expand). High IV rank is
    penalized (you overpay and face vega crush after catalysts).
  * For SHORT-premium theses (selling spreads), we prefer HIGH IV rank
    (richer premium to collect).

Returns a `SignalScore` where score = how favorable IV is for the given stance.
"""

from __future__ import annotations

from app.domain.enums import Direction
from app.domain.options import IVContext
from app.domain.signals import SignalScore

_LONG_PREMIUM = {Direction.BULLISH, Direction.BEARISH, Direction.VOL_LONG}
_SHORT_PREMIUM = {Direction.VOL_SHORT}


def analyze_volatility(iv: IVContext, stance: Direction) -> SignalScore:
    iv_rank = iv.iv_rank
    ratio = iv.iv_hv_ratio  # IV/HV: >1 means options rich vs realized

    if iv_rank is None:
        return SignalScore(
            name="volatility",
            score=0.3,
            confidence=0.2,
            rationale="IV rank unavailable; neutral-cautious.",
            details={"iv_rank": None},
        )

    if stance in _SHORT_PREMIUM:
        # Selling premium: reward high IV rank.
        score = iv_rank
        note = f"IV rank {iv_rank:.0%} favors premium selling."
    else:
        # Buying premium (default): reward low/moderate IV rank.
        # Peak favorability around IV rank 25-40%; taper toward extremes.
        score = max(0.0, 1.0 - iv_rank)
        if iv_rank > 0.6:
            note = f"IV rank {iv_rank:.0%} is elevated — long premium overpays."
        elif iv_rank < 0.15:
            note = f"IV rank {iv_rank:.0%} is very low — cheap premium, but confirm a catalyst."
        else:
            note = f"IV rank {iv_rank:.0%} is reasonable for buying premium."

    # A very rich IV/HV ratio warns of mean-reversion risk for long premium.
    if ratio is not None and stance not in _SHORT_PREMIUM and ratio > 1.5:
        score *= 0.85
        note += f" IV/HV {ratio:.2f} rich."

    return SignalScore(
        name="volatility",
        score=round(min(1.0, max(0.0, score)), 4),
        direction=stance,
        confidence=0.6,
        rationale=note,
        details={
            "iv_rank": round(iv_rank, 3),
            "iv30": iv.iv30,
            "hv20": iv.hv20,
            "iv_hv_ratio": round(ratio, 3) if ratio is not None else None,
        },
    )
