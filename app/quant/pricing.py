"""Black-Scholes pricing and greeks — the single pricing model used everywhere.

Providers (including the mock chain), the engine, and the backtester all price
through these functions so entry marks, sizing, and repricing are internally
consistent. Inconsistent pricing across components silently corrupts backtests,
so there is exactly one model.
"""

from __future__ import annotations

import math

from app.domain.enums import OptionAction, OptionType
from app.domain.trades import TradePlan

_SQRT = math.sqrt


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT(2.0)))


def _d1_d2(spot: float, strike: float, t: float, vol: float, rate: float) -> tuple[float, float]:
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t) / (vol * _SQRT(t))
    return d1, d1 - vol * _SQRT(t)


def black_scholes_price(
    spot: float,
    strike: float,
    t_years: float,
    vol: float,
    option_type: OptionType,
    rate: float = 0.04,
) -> float:
    """Black-Scholes price per share. Returns intrinsic at/after expiry."""
    if t_years <= 0 or vol <= 0 or spot <= 0:
        if option_type == OptionType.CALL:
            return max(0.0, spot - strike)
        return max(0.0, strike - spot)

    d1, d2 = _d1_d2(spot, strike, t_years, vol, rate)
    disc = math.exp(-rate * t_years)
    if option_type == OptionType.CALL:
        return spot * norm_cdf(d1) - strike * disc * norm_cdf(d2)
    return strike * disc * norm_cdf(-d2) - spot * norm_cdf(-d1)


def black_scholes_delta(
    spot: float,
    strike: float,
    t_years: float,
    vol: float,
    option_type: OptionType,
    rate: float = 0.04,
) -> float:
    """Option delta. Falls back to a 0/1 step at expiry."""
    if t_years <= 0 or vol <= 0 or spot <= 0:
        itm = (spot > strike) if option_type == OptionType.CALL else (spot < strike)
        base = 1.0 if itm else 0.0
        return base if option_type == OptionType.CALL else -base
    d1, _ = _d1_d2(spot, strike, t_years, vol, rate)
    if option_type == OptionType.CALL:
        return norm_cdf(d1)
    return norm_cdf(d1) - 1.0


def net_position_price(
    plan: TradePlan,
    spot: float,
    days_to_expiry: int,
    vol: float,
    rate: float = 0.04,
) -> float:
    """Per-share net value of a plan's legs (BUY adds, SELL subtracts)."""
    t = max(0.0, days_to_expiry) / 365.0
    net = 0.0
    for leg in plan.legs:
        price = black_scholes_price(spot, leg.strike, t, vol, leg.option_type, rate)
        if leg.action in (OptionAction.BUY_TO_OPEN, OptionAction.BUY_TO_CLOSE):
            net += price
        else:
            net -= price
    return round(net, 4)


def plan_entry_net_per_share(plan: TradePlan) -> float:
    """Net per-share entry price implied by the plan legs (debit > 0)."""
    net = 0.0
    for leg in plan.legs:
        if leg.action in (OptionAction.BUY_TO_OPEN, OptionAction.BUY_TO_CLOSE):
            net += leg.entry_price
        else:
            net -= leg.entry_price
    return round(net, 4)
