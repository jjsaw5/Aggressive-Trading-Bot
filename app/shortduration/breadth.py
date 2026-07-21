"""Watchlist participation — a PROXY, explicitly NOT market breadth.

This measures the share of *our own tracked universe* trading above session VWAP
(and above opening range). It is a weak, watchlist-specific signal, easily skewed
by a tech-heavy universe — it is NOT exchange breadth (advance/decline, up/down
volume, TICK). Real market internals live in `app/providers/internals.py` and the
`MarketInternals` model. Every consumer and the UI must present this as
participation, never as "market breadth", and it must not act as a strong regime
gate on its own.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.domain.shortduration import IntradayLevels


class WatchlistParticipation(BaseModel):
    symbols_considered: int
    above_vwap: int
    above_opening_range: int
    above_vwap_pct: float | None = None  # [0, 1] — share of the WATCHLIST above VWAP
    above_opening_range_pct: float | None = None
    is_proxy: bool = True
    note: str = "Watchlist participation (our tracked universe), not exchange breadth."


def compute_participation(levels: list[IntradayLevels]) -> WatchlistParticipation:
    """Aggregate per-symbol VWAP/opening-range posture into a participation proxy.

    Only symbols with a decisive reading count toward the denominator, so a
    universe that is mostly not-yet-established doesn't silently read as bearish.
    """
    vwap_votes = [lv.above_vwap for lv in levels if lv.above_vwap is not None]
    or_votes = [lv.above_opening_range for lv in levels if lv.above_opening_range is not None]
    above_vwap = sum(1 for v in vwap_votes if v)
    above_or = sum(1 for v in or_votes if v)
    return WatchlistParticipation(
        symbols_considered=len(levels),
        above_vwap=above_vwap,
        above_opening_range=above_or,
        above_vwap_pct=round(above_vwap / len(vwap_votes), 3) if vwap_votes else None,
        above_opening_range_pct=round(above_or / len(or_votes), 3) if or_votes else None,
    )


# Back-compat aliases (one release) — old names resolve to the renamed symbols so
# existing imports keep working while call sites migrate.
BreadthProxy = WatchlistParticipation
compute_breadth = compute_participation
