"""Causal entry-time feature extraction for the feature-validation harness.

Conviction-Scanner spec §3: conviction is EARNED, so before any feature can carry
weight it must be (a) computable from information available AT ENTRY (no
look-ahead) and (b) shown to predict a net-of-cost outcome out-of-sample. This
module handles (a) — turning a corpus trade + its recorded contract histories
into a feature vector read only from bars on/before the entry date.

Features are deliberately price/vol/structure/cost only. The UW tier has no
historic flow-alerts pre-2023, and the corpus does not carry flow, so no flow
feature is fabricated here — that caveat is honored, not hidden. Flow features
enter only when a flow-bearing corpus exists (spec §8, parked).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from app.backtest.real_mark_seed import _iv_rank_proxy
from app.domain.historic import HistoricOptionBar

# Feature keys and whether each is numeric (rank-correlated raw) or categorical
# (target-encoded on train). Kept explicit so a registry can't silently gain a
# feature that was never validated.
NUMERIC_FEATURES = ("dte", "iv_rank", "iv_level", "entry_spread_pct", "spot_momentum")
CATEGORICAL_FEATURES = ("direction", "structure")
ALL_FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES


@dataclass(frozen=True)
class FeatureVector:
    trade_id: str
    entry_date: date
    exit_date: date | None
    vol_regime: str
    net_pnl: float  # net-of-cost P&L at k=1.0 (the conservative, full-spread label)
    values: dict[str, float | str | None] = field(default_factory=dict)

    @property
    def win(self) -> int:
        return 1 if self.net_pnl > 0 else 0


def _bar_on(bars: list[HistoricOptionBar], d: date) -> HistoricOptionBar | None:
    return next((b for b in bars if b.date == d), None)


def _leg_spread_pct(bars: list[HistoricOptionBar], d: date) -> float | None:
    b = _bar_on(bars, d)
    if b is None or b.nbbo_bid is None or b.nbbo_ask is None:
        return None
    mid = (b.nbbo_bid + b.nbbo_ask) / 2
    if mid <= 0:
        return None
    return (b.nbbo_ask - b.nbbo_bid) / mid


def _spot_momentum(spot_path: dict[date, float], entry: date, lookback: int = 20) -> float | None:
    """Trailing return of the reconstructed spot over ~`lookback` trading days ending
    at entry — the trend strength the entry saw, read only from past bars."""
    past = sorted(d for d in spot_path if d <= entry)
    if len(past) < 5:
        return None
    cur = spot_path[past[-1]]
    ref_date = past[max(0, len(past) - 1 - lookback)]
    ref = spot_path[ref_date]
    if not ref:
        return None
    return round((cur - ref) / ref, 5)


def extract_features(
    trade,
    result,
    *,
    hist: dict[str, list[HistoricOptionBar]],
    spot_path: dict[date, float],
    atm_call_bars: list[HistoricOptionBar],
) -> FeatureVector | None:
    """Build a causal feature vector for one included, priced trade. Returns None if
    the trade was excluded or has no conservative net P&L (nothing to label)."""
    if not result.included:
        return None
    net = result.net_pnl_conservative
    if net is None:
        return None
    entry = trade.entry_date
    long_bars = hist.get(trade.long_id, [])
    short_bars = hist.get(trade.short_id, [])
    ls = _leg_spread_pct(long_bars, entry)
    ss = _leg_spread_pct(short_bars, entry)
    entry_spread_pct = None
    if ls is not None and ss is not None:
        entry_spread_pct = round((ls + ss) / 2, 5)
    entry_bar = _bar_on(atm_call_bars, entry)
    iv_level = entry_bar.iv if entry_bar else None
    values: dict[str, float | str | None] = {
        "dte": float(trade.dte_at_entry),
        "iv_rank": _iv_rank_proxy(atm_call_bars, entry),
        "iv_level": iv_level,
        "entry_spread_pct": entry_spread_pct,
        "spot_momentum": _spot_momentum(spot_path, entry),
        "direction": trade.direction,
        "structure": trade.strategy,
    }
    return FeatureVector(
        trade_id=trade.trade_id, entry_date=entry, exit_date=result.exit_date,
        vol_regime=trade.vol_regime or "unknown", net_pnl=net, values=values,
    )


def exit_or_entry(fv: FeatureVector) -> date:
    """Resolution date for walk-forward purge (falls back to entry if never exited)."""
    return fv.exit_date or (fv.entry_date + timedelta(days=1))
