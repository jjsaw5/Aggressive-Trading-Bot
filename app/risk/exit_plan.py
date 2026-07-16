"""Exit-plan engine — turn a position into concrete, mechanical exit levels.

Answers platform questions 11 & 12 (when to take profit / when to close) with
actual NET PRICES to work, not just percentages. Works for:

  * debit verticals (bull call / bear put): take profit at % of MAX profit,
    stop at % of the debit, time-stop by DTE;
  * credit verticals (bull put / bear call) and iron condors: take profit at %
    of the CREDIT captured, stop at a multiple of the credit;
  * long single options / straddles / strangles: take profit at % gain on the
    premium, stop trailing back toward cost.

The builders take primitives so the same engine prices exits for BRAND-NEW
suggestions (via `for_trade_plan`) and for EXISTING broker positions (call a
builder directly with the entry debit/credit + width).

Key idea it encodes: you do NOT need to hold a spread to expiration. Capturing
50-75% of max and leaving is higher risk-adjusted return than chasing the last
scraps through the high-gamma final week.
"""

from __future__ import annotations

from app.domain.enums import StrategyType
from app.domain.trades import ExitLevel, ExitPlan, TradePlan

DEFAULT_TP_FRACS = (0.5, 0.75)
DEFAULT_STOP_FRAC = 0.5
DEFAULT_TIME_STOP_DTE = 7

_DEBIT_VERTICALS = {StrategyType.BULL_CALL_SPREAD, StrategyType.BEAR_PUT_SPREAD}
_CREDIT_VERTICALS = {StrategyType.BULL_PUT_SPREAD, StrategyType.BEAR_CALL_SPREAD}
_LONG_SINGLES = {
    StrategyType.LONG_CALL,
    StrategyType.LONG_PUT,
    StrategyType.LONG_STRADDLE,
    StrategyType.LONG_STRANGLE,
}


def debit_vertical_exit(
    debit: float,
    width: float,
    contracts: int,
    *,
    tp_fracs: tuple[float, ...] = DEFAULT_TP_FRACS,
    stop_frac: float = DEFAULT_STOP_FRAC,
    time_stop_dte: int = DEFAULT_TIME_STOP_DTE,
    breakevens: tuple[float, ...] = (),
) -> ExitPlan:
    """Debit spread: close by SELLING the spread. `debit`/`width` per share."""
    max_profit_ps = max(0.0, width - debit)
    levels: list[ExitLevel] = []
    for p in tp_fracs:
        net = round(debit + p * max_profit_ps, 2)
        levels.append(
            ExitLevel(
                kind="take_profit",
                label=f"Take profit ({int(p * 100)}% of max)",
                net_price=net,
                pnl_usd=round(p * max_profit_ps * 100 * contracts, 2),
                note="Sell-to-close limit at this net price.",
            )
        )
    stop_net = round(debit * (1 - stop_frac), 2)
    levels.append(
        ExitLevel(
            kind="stop",
            label=f"Stop (-{int(stop_frac * 100)}% of debit)",
            net_price=stop_net,
            pnl_usd=round(-stop_frac * debit * 100 * contracts, 2),
            note="Sell-to-close if the spread decays to here.",
        )
    )
    levels.append(
        ExitLevel(
            kind="time_stop",
            label="Time stop",
            net_price=None,
            pnl_usd=None,
            note=f"Close or roll by {time_stop_dte} DTE regardless of price.",
        )
    )
    return ExitPlan(
        method="debit_vertical",
        action="sell_to_close",
        entry_net_per_share=round(debit, 2),
        contracts=contracts,
        max_profit_usd=round(max_profit_ps * 100 * contracts, 2),
        max_loss_usd=round(debit * 100 * contracts, 2),
        breakevens=list(breakevens),
        time_stop_dte=time_stop_dte,
        levels=levels,
    )


def credit_vertical_exit(
    credit: float,
    width: float,
    contracts: int,
    *,
    tp_fracs: tuple[float, ...] = DEFAULT_TP_FRACS,
    stop_multiple: float = 1.0,
    time_stop_dte: int = DEFAULT_TIME_STOP_DTE,
    breakevens: tuple[float, ...] = (),
) -> ExitPlan:
    """Credit spread / condor: close by BUYING the spread back. `credit`/`width`
    per share. Take profit at % of credit captured; stop at a MULTIPLE of the
    credit lost (default 1x = spread doubled)."""
    max_loss_ps = max(0.0, width - credit)
    levels: list[ExitLevel] = []
    for p in tp_fracs:
        net = round(credit * (1 - p), 2)  # buy back cheaper than you sold
        levels.append(
            ExitLevel(
                kind="take_profit",
                label=f"Take profit ({int(p * 100)}% of credit)",
                net_price=net,
                pnl_usd=round(p * credit * 100 * contracts, 2),
                note="Buy-to-close limit at this net price.",
            )
        )
    stop_net = round(min(width, credit * (1 + stop_multiple)), 2)
    stop_loss_ps = min(max_loss_ps, stop_net - credit)
    levels.append(
        ExitLevel(
            kind="stop",
            label=f"Stop ({stop_multiple:g}x credit)",
            net_price=stop_net,
            pnl_usd=round(-stop_loss_ps * 100 * contracts, 2),
            note="Buy-to-close if the spread widens to here.",
        )
    )
    levels.append(
        ExitLevel(
            kind="time_stop",
            label="Time stop",
            net_price=None,
            pnl_usd=None,
            note=f"Close or roll by {time_stop_dte} DTE (gamma/assignment risk).",
        )
    )
    return ExitPlan(
        method="credit_vertical",
        action="buy_to_close",
        entry_net_per_share=round(credit, 2),
        contracts=contracts,
        max_profit_usd=round(credit * 100 * contracts, 2),
        max_loss_usd=round(max_loss_ps * 100 * contracts, 2),
        breakevens=list(breakevens),
        time_stop_dte=time_stop_dte,
        levels=levels,
    )


def long_option_exit(
    premium: float,
    contracts: int,
    *,
    tp_fracs: tuple[float, ...] = (0.5, 1.0),
    stop_frac: float = DEFAULT_STOP_FRAC,
    time_stop_dte: int = DEFAULT_TIME_STOP_DTE,
    breakevens: tuple[float, ...] = (),
) -> ExitPlan:
    """Long single option / straddle / strangle: uncapped upside, full premium
    at risk. Take profit at % GAIN on premium; stop at -% of premium (or trail
    to cost). Close by SELLING."""
    levels: list[ExitLevel] = []
    for p in tp_fracs:
        net = round(premium * (1 + p), 2)
        levels.append(
            ExitLevel(
                kind="take_profit",
                label=f"Take profit (+{int(p * 100)}%)",
                net_price=net,
                pnl_usd=round(p * premium * 100 * contracts, 2),
                note="Sell-to-close / scale out at this mark.",
            )
        )
    stop_net = round(premium * (1 - stop_frac), 2)
    levels.append(
        ExitLevel(
            kind="stop",
            label=f"Stop (-{int(stop_frac * 100)}%)",
            net_price=stop_net,
            pnl_usd=round(-stop_frac * premium * 100 * contracts, 2),
            note="Sell-to-close here; trail up toward cost once in profit.",
        )
    )
    levels.append(
        ExitLevel(
            kind="time_stop",
            label="Time stop",
            net_price=None,
            pnl_usd=None,
            note=f"Close by {time_stop_dte} DTE — theta accelerates into expiry.",
        )
    )
    return ExitPlan(
        method="long_option",
        action="sell_to_close",
        entry_net_per_share=round(premium, 2),
        contracts=contracts,
        max_profit_usd=None,  # uncapped
        max_loss_usd=round(premium * 100 * contracts, 2),
        breakevens=list(breakevens),
        time_stop_dte=time_stop_dte,
        levels=levels,
    )


def _vertical_width(plan: TradePlan) -> float:
    strikes = [leg.strike for leg in plan.legs]
    return abs(max(strikes) - min(strikes)) if len(strikes) >= 2 else 0.0


def for_trade_plan(plan: TradePlan) -> ExitPlan:
    """Build an ExitPlan for a platform-generated TradePlan, reading the entry
    debit/credit, width, and breakevens off the plan + its analytics."""
    contracts = plan.contracts
    per_share = plan.net_debit / 100.0  # signed dollars -> per-share
    tp_frac = plan.risk.profit_target_pct
    stop_frac = plan.risk.stop_loss_pct
    tstop = plan.risk.time_stop_dte or DEFAULT_TIME_STOP_DTE
    bes = tuple(plan.analytics.breakevens) if plan.analytics else ()
    tp_fracs = (tp_frac, min(0.95, tp_frac + 0.25))

    if plan.strategy in _DEBIT_VERTICALS:
        return debit_vertical_exit(
            per_share, _vertical_width(plan), contracts,
            tp_fracs=tp_fracs, stop_frac=stop_frac, time_stop_dte=tstop, breakevens=bes,
        )
    if plan.strategy in _CREDIT_VERTICALS or plan.strategy == StrategyType.IRON_CONDOR:
        credit = -per_share  # net_debit is negative for credits
        # For a condor, use the wider wing as the effective width.
        return credit_vertical_exit(
            credit, _vertical_width(plan), contracts,
            tp_fracs=tp_fracs, time_stop_dte=tstop, breakevens=bes,
        )
    if plan.strategy in _LONG_SINGLES:
        return long_option_exit(
            per_share, contracts,
            tp_fracs=(tp_frac, 1.0), stop_frac=stop_frac, time_stop_dte=tstop, breakevens=bes,
        )
    # Fallback: treat as long premium.
    return long_option_exit(per_share, contracts, time_stop_dte=tstop, breakevens=bes)
