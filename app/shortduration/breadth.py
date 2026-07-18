"""Market-breadth PROXY for short-duration regime decisions.

We do not have a true market-internals feed (NYSE advance/decline, up/down
volume, TICK). Until one is licensed, breadth is approximated from our own liquid
universe: the share of tracked symbols trading above their session VWAP and above
their opening range. This is a transparent proxy — every consumer and the UI must
label it as such and never present it as true exchange breadth.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.domain.shortduration import IntradayLevels


class BreadthProxy(BaseModel):
    symbols_considered: int
    above_vwap: int
    above_opening_range: int
    above_vwap_pct: float | None = None  # [0, 1]
    above_opening_range_pct: float | None = None
    is_proxy: bool = True
    note: str = "Proxy from tracked universe (not exchange advance/decline)."


def compute_breadth(levels: list[IntradayLevels]) -> BreadthProxy:
    """Aggregate per-symbol VWAP/opening-range posture into a breadth proxy.

    Only symbols with a decisive reading count toward the denominator, so a
    universe that is mostly not-yet-established doesn't silently read as bearish.
    """
    vwap_votes = [lv.above_vwap for lv in levels if lv.above_vwap is not None]
    or_votes = [lv.above_opening_range for lv in levels if lv.above_opening_range is not None]
    above_vwap = sum(1 for v in vwap_votes if v)
    above_or = sum(1 for v in or_votes if v)
    return BreadthProxy(
        symbols_considered=len(levels),
        above_vwap=above_vwap,
        above_opening_range=above_or,
        above_vwap_pct=round(above_vwap / len(vwap_votes), 3) if vwap_votes else None,
        above_opening_range_pct=round(above_or / len(or_votes), 3) if or_votes else None,
    )
