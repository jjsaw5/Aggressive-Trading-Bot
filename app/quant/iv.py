"""Implied-volatility analytics: IV rank / percentile, ATM IV from a chain, and
a realized-volatility series used as a transparent proxy when no IV history is
available.

IV rank and IV percentile are *computed* here from an actual volatility history
— they are never taken as an opaque provider field. This makes the volatility
signal auditable and consistent across data sources.

Definitions:
  * IV rank       = (current - min) / (max - min) over the lookback window.
  * IV percentile = fraction of historical observations <= current.
"""

from __future__ import annotations

import math

from app.domain.options import OptionChain

_TRADING_DAYS = 252


def iv_rank(current: float, history: list[float]) -> float | None:
    """Where current sits between the min and max of the history, in [0, 1]."""
    vals = [h for h in history if h is not None and h > 0]
    if not vals:
        return None
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return 0.5  # flat history — treat as mid
    return round(min(1.0, max(0.0, (current - lo) / (hi - lo))), 4)


def iv_percentile(current: float, history: list[float]) -> float | None:
    """Fraction of historical observations at or below current, in [0, 1]."""
    vals = [h for h in history if h is not None and h > 0]
    if not vals:
        return None
    below = sum(1 for v in vals if v <= current)
    return round(below / len(vals), 4)


def realized_vol(closes: list[float], window: int = 20) -> float | None:
    """Annualized realized volatility over the last `window` returns."""
    if len(closes) < window + 1:
        return None
    seg = closes[-(window + 1) :]
    rets = [math.log(seg[k] / seg[k - 1]) for k in range(1, len(seg)) if seg[k - 1] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(_TRADING_DAYS)


def realized_vol_series(closes: list[float], window: int = 20) -> list[float]:
    """Rolling annualized realized-vol series (one point per day once enough
    history exists). Used as an HV proxy for IV rank when no IV history feed is
    configured."""
    out: list[float] = []
    for end in range(window, len(closes)):
        seg = closes[end - window : end + 1]
        rets = [math.log(seg[k] / seg[k - 1]) for k in range(1, len(seg)) if seg[k - 1] > 0]
        if len(rets) < 2:
            continue
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        out.append(math.sqrt(var) * math.sqrt(_TRADING_DAYS))
    return out


def atm_iv_from_chain(chain: OptionChain, dte_target: int = 30) -> float | None:
    """Average call/put implied vol of the near-ATM contracts closest to a
    target DTE — a robust 'current IV' read straight from the live chain, rather
    than trusting a provider's summary field."""
    if not chain.contracts:
        return None
    spot = chain.underlying_price
    if not spot:
        # Fall back to the median strike as an ATM proxy.
        strikes = sorted({c.strike for c in chain.contracts})
        spot = strikes[len(strikes) // 2] if strikes else None
    if not spot:
        return None

    as_of = chain.as_of.date()
    # Pick the expiration whose DTE is closest to the target.
    exps = {c.expiration for c in chain.contracts}
    if not exps:
        return None
    target_exp = min(exps, key=lambda e: abs((e - as_of).days - dte_target))

    near = [
        c
        for c in chain.contracts
        if c.expiration == target_exp and c.implied_volatility and c.implied_volatility > 0
    ]
    if not near:
        return None
    # Take the few strikes nearest spot.
    near.sort(key=lambda c: abs(c.strike - spot))
    sample = near[: min(6, len(near))]
    ivs = [c.implied_volatility for c in sample if c.implied_volatility]
    return round(sum(ivs) / len(ivs), 4) if ivs else None
