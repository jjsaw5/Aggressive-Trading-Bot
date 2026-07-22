"""Normalized per-contract historical option bar (one trading day).

The row model behind real-mark backtesting. Every field arrives from Unusual
Whales' `/api/option-contract/{id}/historic` endpoint as a *string* and is parsed
defensively: a value that cannot be parsed becomes ``None`` — never ``0.0``, and
never a fabricated or `now()`-stamped placeholder. A day with no usable NBBO is
simply not tradeable that day; that is the honest outcome, not an error to paper
over.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


def parse_float(v: object) -> float | None:
    """UW returns numbers as strings (``"0.03"``, ``"0.3105029"``). Parse to
    float; on any failure return None (never 0.0, never a fabricated value)."""
    if v is None:
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_int(v: object) -> int | None:
    f = parse_float(v)
    return int(f) if f is not None else None


@dataclass(frozen=True)
class HistoricOptionBar:
    """One closed trading day of a single option contract's history."""

    contract_id: str  # ISO/OCC option symbol
    date: date  # the trading day (date only)
    nbbo_bid: float | None
    nbbo_ask: float | None
    last_fill: float | None = None  # last_price — reference only, NOT a fill price
    iv: float | None = None  # implied_volatility (last transaction)
    iv_high: float | None = None
    iv_low: float | None = None
    open_interest: int | None = None
    volume: int | None = None
    trades: int | None = None
    # Flow-side daily volumes (for the reconstructed EOD flow proxy). Optional so
    # nothing that only needs pricing has to populate them.
    ask_volume: int | None = None
    bid_volume: int | None = None
    sweep_volume: int | None = None
    total_premium: float | None = None
    option_type: str | None = None  # "C" | "P", from the OCC symbol
    source: str = "unusual_whales"

    @property
    def mid(self) -> float | None:
        """NBBO midpoint, or None when either side is missing or the quote is
        crossed (ask < bid = garbage). A None mid means the day is not tradeable."""
        if self.nbbo_bid is None or self.nbbo_ask is None:
            return None
        if self.nbbo_ask < self.nbbo_bid:
            return None
        return (self.nbbo_bid + self.nbbo_ask) / 2

    @property
    def half_spread(self) -> float | None:
        """Half the bid/ask spread (the most you cross to fill), or None when the
        quote is missing or crossed."""
        if self.nbbo_bid is None or self.nbbo_ask is None:
            return None
        s = (self.nbbo_ask - self.nbbo_bid) / 2
        return s if s >= 0 else None
