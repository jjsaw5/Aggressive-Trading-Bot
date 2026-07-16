"""Build an `IVContext` with a COMPUTED IV rank/percentile.

Source priority:
  1. Real IV history (an `IVHistoryProvider`) -> true IV rank/percentile.
  2. Realized-volatility proxy from real underlying price history -> HV-rank,
     a transparent stand-in when no IV-history feed is configured.
  3. Nothing -> rank left unknown (the volatility scorer degrades cautiously).

`iv_rank_source` records which path was used so every candidate is auditable.
"""

from __future__ import annotations

from datetime import datetime

from app.domain.market import PriceHistory
from app.domain.options import IVContext, IVHistory
from app.quant.iv import iv_percentile, iv_rank, realized_vol, realized_vol_series

_MIN_HISTORY = 30


def build_iv_context(
    symbol: str,
    current_iv: float | None,
    as_of: datetime,
    *,
    iv_history: IVHistory | None = None,
    price_history: PriceHistory | None = None,
    term_structure_slope: float | None = None,
) -> IVContext:
    hv20 = realized_vol(price_history.closes, 20) if price_history else None

    rank: float | None = None
    pct: float | None = None
    source: str | None = None

    if current_iv is not None and iv_history and len(iv_history.ivs) >= _MIN_HISTORY:
        rank = iv_rank(current_iv, iv_history.ivs)
        pct = iv_percentile(current_iv, iv_history.ivs)
        source = "iv_history"
    elif price_history is not None:
        series = realized_vol_series(price_history.closes, 20)
        current_hv = hv20
        if series and current_hv is not None and len(series) >= _MIN_HISTORY:
            rank = iv_rank(current_hv, series)
            pct = iv_percentile(current_hv, series)
            source = "hv_proxy"

    return IVContext(
        symbol=symbol.upper(),
        iv30=round(current_iv, 4) if current_iv is not None else None,
        iv_rank=rank,
        iv_percentile=pct,
        hv20=round(hv20, 4) if hv20 is not None else None,
        term_structure_slope=term_structure_slope,
        iv_rank_source=source,
        as_of=as_of,
        source="computed",
    )
