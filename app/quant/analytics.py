"""Structure analytics: net greeks, breakevens, and probability of profit.

Computed from a `TradePlan`'s legs plus current spot/vol, so every proposed
structure carries an auditable risk/greek profile and a risk-neutral
probability of finishing profitable. Works for single options, debit/credit
verticals, straddles/strangles, and iron condors.
"""

from __future__ import annotations

from datetime import date

from app.domain.enums import OptionAction, OptionType, StrategyType
from app.domain.trades import ContractLeg, SpreadAnalytics, TradePlan
from app.quant.pricing import black_scholes_greeks, prob_below

_BUY = {OptionAction.BUY_TO_OPEN, OptionAction.BUY_TO_CLOSE}


def _leg_t(leg: ContractLeg, as_of: date) -> float:
    return max(0.0, (leg.expiration - as_of).days) / 365.0


def _net_per_share(plan: TradePlan) -> float:
    """Positive = net debit paid; negative = net credit received."""
    net = 0.0
    for leg in plan.legs:
        net += leg.entry_price if leg.action in _BUY else -leg.entry_price
    return round(net, 4)


def net_greeks(plan: TradePlan, spot: float, vol: float, as_of: date, rate: float = 0.04) -> dict:
    delta = gamma = theta = vega = 0.0
    for leg in plan.legs:
        g = black_scholes_greeks(spot, leg.strike, _leg_t(leg, as_of), vol, leg.option_type, rate)
        sign = 1.0 if leg.action in _BUY else -1.0
        scale = sign * leg.quantity * 100.0  # shares per contract
        delta += g["delta"] * scale
        gamma += g["gamma"] * scale
        theta += g["theta"] * scale
        vega += g["vega"] * scale
    return {
        "net_delta": round(delta, 2),
        "net_gamma": round(gamma, 4),
        "net_theta": round(theta, 2),
        "net_vega": round(vega, 2),
    }


def _legs(plan: TradePlan, otype: OptionType, action_buy: bool) -> list[ContractLeg]:
    return [
        leg
        for leg in plan.legs
        if leg.option_type == otype and (leg.action in _BUY) == action_buy
    ]


def breakevens_and_pop(
    plan: TradePlan, spot: float, vol: float, as_of: date, rate: float = 0.04
) -> tuple[list[float], float | None]:
    """Breakeven price(s) and risk-neutral probability of profit by strategy."""
    net = _net_per_share(plan)  # debit>0, credit<0
    t = min((_leg_t(leg, as_of) for leg in plan.legs), default=0.0)
    s = plan.strategy

    def p_above(level: float) -> float:
        return 1.0 - prob_below(spot, level, t, vol, rate)

    def p_below(level: float) -> float:
        return prob_below(spot, level, t, vol, rate)

    if s in (StrategyType.LONG_CALL, StrategyType.BULL_CALL_SPREAD):
        k = min(leg.strike for leg in _legs(plan, OptionType.CALL, True))
        be = round(k + net, 2)
        return [be], round(p_above(be), 4)

    if s in (StrategyType.LONG_PUT, StrategyType.BEAR_PUT_SPREAD):
        k = max(leg.strike for leg in _legs(plan, OptionType.PUT, True))
        be = round(k - net, 2)
        return [be], round(p_below(be), 4)

    if s == StrategyType.BULL_PUT_SPREAD:  # credit; net < 0
        ks = max(leg.strike for leg in _legs(plan, OptionType.PUT, False))
        be = round(ks + net, 2)  # net negative -> below short strike
        return [be], round(p_above(be), 4)

    if s == StrategyType.BEAR_CALL_SPREAD:  # credit
        ks = min(leg.strike for leg in _legs(plan, OptionType.CALL, False))
        be = round(ks - net, 2)  # net negative -> above short strike
        return [be], round(p_below(be), 4)

    if s == StrategyType.LONG_STRADDLE:
        k = _legs(plan, OptionType.CALL, True)[0].strike
        lo, hi = round(k - net, 2), round(k + net, 2)
        pop = round(p_below(lo) + p_above(hi), 4)
        return [lo, hi], pop

    if s == StrategyType.LONG_STRANGLE:
        kp = _legs(plan, OptionType.PUT, True)[0].strike
        kc = _legs(plan, OptionType.CALL, True)[0].strike
        lo, hi = round(kp - net, 2), round(kc + net, 2)
        pop = round(p_below(lo) + p_above(hi), 4)
        return [lo, hi], pop

    if s == StrategyType.IRON_CONDOR:  # credit
        kps = max(leg.strike for leg in _legs(plan, OptionType.PUT, False))
        kcs = min(leg.strike for leg in _legs(plan, OptionType.CALL, False))
        credit = -net
        lo, hi = round(kps - credit, 2), round(kcs + credit, 2)
        pop = round(p_below(hi) - p_below(lo), 4)
        return [lo, hi], max(0.0, pop)

    return [], None


def compute_analytics(
    plan: TradePlan, spot: float, vol: float, as_of: date, rate: float = 0.04
) -> SpreadAnalytics:
    greeks = net_greeks(plan, spot, vol, as_of, rate)
    bes, pop = breakevens_and_pop(plan, spot, vol, as_of, rate)

    ev: float | None = None
    max_profit = plan.risk.max_profit_usd
    if pop is not None and max_profit is not None:
        ev = round(pop * max_profit - (1.0 - pop) * plan.risk.max_loss_usd, 2)

    return SpreadAnalytics(
        breakevens=bes,
        probability_of_profit=pop,
        expected_value_usd=ev,
        net_delta=greeks["net_delta"],
        net_gamma=greeks["net_gamma"],
        net_theta=greeks["net_theta"],
        net_vega=greeks["net_vega"],
        is_credit=_net_per_share(plan) < 0,
    )
