"""Trade-plan construction: turn a chosen contract into a sized, defined-risk
plan with explicit entry, exits, invalidation, and profit-taking rules.

Answers the plan-side platform questions:
  9.  What is the maximum defined risk?      -> RiskPlan.max_loss_usd
  10. What invalidates the trade?            -> RiskPlan.invalidation_note
  11. When should profits be taken?          -> RiskPlan.profit_target_pct
  12. When should the trade be closed?       -> stop_loss_pct / time_stop_dte
  13. How does it affect account risk?       -> RiskPlan.account_risk_pct (+ portfolio)
"""

from __future__ import annotations

from datetime import date

from app.domain.enums import Direction, OptionAction, OptionType, StrategyType
from app.domain.options import OptionContract
from app.domain.trades import ContractLeg, RiskPlan, TradePlan
from app.engine.contract_selection import SpreadChoice
from app.risk.policy import RiskPolicy
from app.risk.position_sizing import size_by_defined_risk


def build_long_option_plan(
    contract: OptionContract,
    direction: Direction,
    policy: RiskPolicy,
    as_of: date,
    *,
    open_risk_usd: float = 0.0,
) -> TradePlan | None:
    """Build a long single-leg option plan. Returns None if not sizeable within
    risk limits (e.g. even one contract exceeds the per-trade cap)."""
    mid = contract.mid
    if mid is None or mid <= 0:
        return None

    per_contract_risk = round(mid * 100, 2)  # long option: debit = max loss
    sizing = size_by_defined_risk(per_contract_risk, policy, open_risk_usd=open_risk_usd)
    if not sizing.is_tradeable:
        return None

    strategy = (
        StrategyType.LONG_CALL if direction == Direction.BULLISH else StrategyType.LONG_PUT
    )
    leg = ContractLeg(
        symbol=contract.symbol,
        option_symbol=contract.option_symbol,
        action=OptionAction.BUY_TO_OPEN,
        option_type=contract.option_type,
        strike=contract.strike,
        expiration=contract.expiration,
        quantity=sizing.contracts,
        entry_price=mid,
    )

    underlying_move = (
        "below" if direction == Direction.BULLISH else "above"
    )
    invalidation = (
        f"Thesis invalid if the underlying closes {underlying_move} the entry "
        f"trend reference or the option loses {int(policy.default_stop_loss_pct * 100)}% "
        f"of its debit; also exit if DTE < {policy.default_time_stop_dte}."
    )

    reward_to_risk = None  # long option upside is open-ended; R:R is scenario-based

    risk = RiskPlan(
        max_loss_usd=sizing.max_loss_usd,
        max_profit_usd=None,
        breakeven=(
            round(contract.strike + mid, 2)
            if direction == Direction.BULLISH
            else round(contract.strike - mid, 2)
        ),
        account_risk_pct=sizing.account_risk_pct,
        reward_to_risk=reward_to_risk,
        profit_target_pct=policy.default_profit_target_pct,
        stop_loss_pct=policy.default_stop_loss_pct,
        time_stop_dte=policy.default_time_stop_dte,
        invalidation_note=invalidation,
    )

    return TradePlan(
        symbol=contract.symbol,
        direction=direction,
        strategy=strategy,
        legs=[leg],
        net_debit=per_contract_risk,  # dollars per 1 lot
        contracts=sizing.contracts,
        risk=risk,
        rationale=(
            f"Long {contract.option_type.value} {contract.strike} exp {contract.expiration}, "
            f"{sizing.contracts} lot(s), defined risk ${sizing.max_loss_usd:.0f} "
            f"({sizing.account_risk_pct:.1%} of equity)."
        ),
    )


def build_vertical_spread_plan(
    spread: SpreadChoice,
    direction: Direction,
    policy: RiskPolicy,
    as_of: date,
    *,
    open_risk_usd: float = 0.0,
) -> TradePlan | None:
    """Build a sized defined-risk debit vertical. Returns None if not sizeable.

    This is the workhorse structure for a small account: per-contract risk is the
    net debit, typically a fraction of an outright long option, so mega-cap names
    become tradeable within a tight per-trade risk cap.
    """
    per_contract_risk = spread.max_loss_per_contract
    sizing = size_by_defined_risk(per_contract_risk, policy, open_risk_usd=open_risk_usd)
    if not sizing.is_tradeable:
        return None

    long_leg, short_leg = spread.long_leg, spread.short_leg
    strategy = (
        StrategyType.BULL_CALL_SPREAD
        if direction == Direction.BULLISH
        else StrategyType.BEAR_PUT_SPREAD
    )
    legs = [
        ContractLeg(
            symbol=long_leg.symbol,
            option_symbol=long_leg.option_symbol,
            action=OptionAction.BUY_TO_OPEN,
            option_type=long_leg.option_type,
            strike=long_leg.strike,
            expiration=long_leg.expiration,
            quantity=sizing.contracts,
            entry_price=long_leg.mid or 0.0,
        ),
        ContractLeg(
            symbol=short_leg.symbol,
            option_symbol=short_leg.option_symbol,
            action=OptionAction.SELL_TO_OPEN,
            option_type=short_leg.option_type,
            strike=short_leg.strike,
            expiration=short_leg.expiration,
            quantity=sizing.contracts,
            entry_price=short_leg.mid or 0.0,
        ),
    ]

    if direction == Direction.BULLISH:
        breakeven = round(long_leg.strike + spread.net_debit_per_share, 2)
    else:
        breakeven = round(long_leg.strike - spread.net_debit_per_share, 2)

    invalidation = (
        f"Thesis invalid if the spread loses {int(policy.default_stop_loss_pct * 100)}% "
        f"of its debit, if the underlying moves decisively against the {direction.value} "
        f"thesis, or if DTE < {policy.default_time_stop_dte}."
    )

    risk = RiskPlan(
        max_loss_usd=sizing.max_loss_usd,
        max_profit_usd=round(spread.max_profit_per_contract * sizing.contracts, 2),
        breakeven=breakeven,
        account_risk_pct=sizing.account_risk_pct,
        reward_to_risk=spread.reward_to_risk,
        profit_target_pct=policy.default_profit_target_pct,
        stop_loss_pct=policy.default_stop_loss_pct,
        time_stop_dte=policy.default_time_stop_dte,
        invalidation_note=invalidation,
    )

    otype = "call" if long_leg.option_type == OptionType.CALL else "put"
    return TradePlan(
        symbol=long_leg.symbol,
        direction=direction,
        strategy=strategy,
        legs=legs,
        net_debit=per_contract_risk,
        contracts=sizing.contracts,
        risk=risk,
        rationale=(
            f"{strategy.value.replace('_', ' ')}: long {otype} {long_leg.strike} / "
            f"short {otype} {short_leg.strike} exp {long_leg.expiration}, "
            f"{sizing.contracts} lot(s), defined risk ${sizing.max_loss_usd:.0f} "
            f"({sizing.account_risk_pct:.1%}), R:R {spread.reward_to_risk}."
        ),
    )
