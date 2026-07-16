"""Catalyst analyzer.

Answers: *is there a catalyst?* Scores the presence and proximity of a known
catalyst (earnings by default). Binary events (e.g. FDA) are flagged so the
candidate builder can penalize or exclude them per universe config.

A near-term catalyst raises opportunity but also risk (IV crush, gap risk); the
score reflects *presence*, while risk handling is the trade plan's job.
"""

from __future__ import annotations

from datetime import date

from app.domain.market import CatalystEvent
from app.domain.signals import SignalScore


def analyze_catalysts(
    symbol: str, catalysts: list[CatalystEvent], as_of: date
) -> SignalScore:
    upcoming = sorted(
        (c for c in catalysts if c.event_date >= as_of),
        key=lambda c: c.event_date,
    )
    if not upcoming:
        return SignalScore(
            name="catalyst",
            score=0.1,
            confidence=0.4,
            rationale="No known near-term catalyst.",
            details={"has_catalyst": False, "is_binary": False},
        )

    nxt = upcoming[0]
    dte = (nxt.event_date - as_of).days
    # Proximity score: peaks a few days to ~2 weeks out; fades far out.
    if dte <= 1:
        proximity = 0.6  # too close for long premium (IV crush risk)
    elif dte <= 14:
        proximity = 1.0 - (dte - 2) * 0.03
    else:
        proximity = max(0.2, 0.6 - (dte - 14) * 0.02)

    is_binary = any(c.is_binary for c in upcoming if c.event_date == nxt.event_date)
    score = round(min(1.0, proximity) * (0.6 if is_binary else 1.0), 4)

    return SignalScore(
        name="catalyst",
        score=score,
        confidence=0.7,
        rationale=(
            f"{nxt.event_type} in {dte}d ({nxt.event_date})"
            + (" [BINARY]" if is_binary else "")
        ),
        details={
            "has_catalyst": True,
            "event_type": nxt.event_type,
            "event_date": nxt.event_date.isoformat(),
            "days_to_event": dte,
            "is_binary": is_binary,
        },
    )
