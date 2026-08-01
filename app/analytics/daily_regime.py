"""Build the daily market-regime table, and look it up without lookahead.

Pre-flight item P6 (reviewer Ruling 2). Two daily series from FMP — `^VIX` and
`^GSPC` — reduced to one row per session and a stable class label.

THE LOOKAHEAD RULE, which is the whole reason this module is not three lines:

A signal fired at 10:15 on session D cannot know D's close. Joining it to D's
regime row would hand the decision information it did not have, and the
pre-registration's per-regime cuts would then be conditioned on the future. So
`regime_as_of` returns the most recent session **strictly before** the signal's
date. Same discipline as `policy_settlement._close_on_or_before`, which never
walks forward.

That choice is conservative in the right direction: it can only ever label a
signal with staler information than a cheat would, never fresher.
"""

from __future__ import annotations

import math
from datetime import date

from app.domain.regime import VIX_PCTL_HIGH, VIX_PCTL_LOW, DailyRegime
from app.logging_config import get_logger

log = get_logger(__name__)

VIX_SYMBOL = "^VIX"
SPX_SYMBOL = "^GSPC"

_VIX_WINDOW = 20
_RV_WINDOW = 20
_SMA_WINDOW = 50
_TRADING_DAYS = 252


def percentile_of_last(values: list[float], window: int = _VIX_WINDOW) -> float | None:
    """Percentile rank of the final value within the trailing `window`, inclusive.

    Returns None on a short window rather than computing over whatever is there —
    a percentile over 6 observations is not the same measurement as one over 20,
    and pooling them would make the threshold mean different things on different
    days.
    """
    if len(values) < window:
        return None
    tail = values[-window:]
    current = tail[-1]
    return round(sum(1 for v in tail if v <= current) / len(tail), 4)


def realized_vol(closes: list[float], window: int = _RV_WINDOW) -> float | None:
    """Annualised stdev of daily log returns. None if the window is short.

    Needs `window` returns, so `window + 1` closes. Sample stdev (n-1), the
    convention `app/quant/iv.py::realized_vol` already uses, so the two are
    comparable.
    """
    if len(closes) < window + 1:
        return None
    rets = []
    for prev, cur in zip(closes[-(window + 1):-1], closes[-window:], strict=True):
        if prev <= 0 or cur <= 0:
            return None  # a non-positive close is bad data, not a big move
        rets.append(math.log(cur / prev))
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return round(math.sqrt(var) * math.sqrt(_TRADING_DAYS), 6)


def vs_sma(closes: list[float], window: int = _SMA_WINDOW) -> float | None:
    """(last close - SMA) / SMA. None if fewer than `window` closes."""
    if len(closes) < window:
        return None
    sma = sum(closes[-window:]) / window
    if sma <= 0:
        return None
    return round((closes[-1] - sma) / sma, 6)


def classify(vix_pctl: float | None, spx_vs_sma: float | None) -> tuple[str, str, str]:
    """(regime_class, vol_state, trend_state).

    Either axis missing makes the whole class `unknown` rather than falling back
    to a middle bucket — a default would silently swell one class with rows that
    were never measured, and the per-regime gate counts those classes.
    """
    if vix_pctl is None:
        vol = "unknown"
    elif vix_pctl < VIX_PCTL_LOW:
        vol = "lowvol"
    elif vix_pctl >= VIX_PCTL_HIGH:
        vol = "highvol"
    else:
        vol = "midvol"

    trend = "unknown" if spx_vs_sma is None else ("above" if spx_vs_sma >= 0 else "below")

    if vol == "unknown" or trend == "unknown":
        return "unknown", vol, trend
    return f"{vol}_{trend}", vol, trend


def build_regime_series(
    vix_by_date: dict[date, float],
    spx_by_date: dict[date, float],
    *,
    source: str = "fmp",
) -> list[DailyRegime]:
    """One row per session on which BOTH series have a close.

    Sessions where either series is missing are skipped, not interpolated: a
    regime row is a joint observation, and half of one is not a reading.
    """
    sessions = sorted(set(vix_by_date) & set(spx_by_date))
    if not sessions:
        return []

    vix_series = [vix_by_date[d] for d in sessions]
    spx_series = [spx_by_date[d] for d in sessions]

    out: list[DailyRegime] = []
    for i, session in enumerate(sessions):
        # Each row sees only data up to and including its own session.
        vix_hist = vix_series[: i + 1]
        spx_hist = spx_series[: i + 1]

        pctl = percentile_of_last(vix_hist, _VIX_WINDOW)
        rv = realized_vol(spx_hist, _RV_WINDOW)
        sma_gap = vs_sma(spx_hist, _SMA_WINDOW)
        cls, vol, trend = classify(pctl, sma_gap)

        out.append(DailyRegime(
            session=session,
            vix_close=round(vix_hist[-1], 4),
            vix_percentile_20d=pctl,
            spx_realized_vol_20d=rv,
            spx_vs_50d_sma=sma_gap,
            regime_class=cls, vol_state=vol, trend_state=trend,
            source=source,
        ))
    return out


def regime_as_of(rows: list[DailyRegime], when: date) -> DailyRegime | None:
    """The regime a decision made on `when` could actually have known.

    STRICTLY BEFORE `when`. A signal fired intraday cannot know its own
    session's close, so joining to it would be lookahead — the per-regime cuts
    would then be conditioned on information the decision did not have.

    Returns None when nothing precedes `when`, rather than reaching forward to
    the nearest available row.
    """
    prior = [r for r in rows if r.session < when]
    return max(prior, key=lambda r: r.session) if prior else None


async def fetch_regime_inputs(lookback_days: int = 400) -> tuple[dict, dict]:
    """(vix_by_date, spx_by_date) from the configured market-data provider.

    Returns empty dicts on failure so the caller reports a gap rather than
    building a table from one series.
    """
    from app.providers import registry

    out: list[dict[date, float]] = []
    for symbol in (VIX_SYMBOL, SPX_SYMBOL):
        try:
            hist = await registry.market_data_provider().get_price_history(
                symbol, lookback_days=lookback_days
            )
            out.append({c.ts.date(): c.close for c in hist.candles if c.close > 0})
        except Exception as exc:  # noqa: BLE001 — a missing series is a reportable gap
            log.warning("regime_series_failed", symbol=symbol, error=str(exc))
            out.append({})
    return out[0], out[1]
