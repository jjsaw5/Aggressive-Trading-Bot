"""The target and the stop must be emitted as one pair, priced off the debit.

Regression for a real loss: an INTC put spread up 66% at the close with a limit
resting at +109% of the debit. It never filled, the underlying gapped 10.5%
overnight, and the trade finished -80%. The target was priced off MAX PROFIT
(the exit_plan PT1 convention) rather than off the DEBIT (what RiskPlan declares
and what policy settlement grades), and no stop was ever placed.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.enums import Direction, OptionAction, OptionType, StrategyType
from app.domain.trades import ContractLeg, RiskPlan, TradePlan
from app.risk.bracket import (
    DEFAULT_STOP_PCT,
    DEFAULT_TARGET_PCT,
    bracket_from_entry,
    bracket_from_plan,
)

_EXP = date(2026, 7, 31)


def _plan(net_debit: float, *, contracts: int = 1, target=0.5, stop=0.5,
          strikes=(83.0, 79.0), otype=OptionType.PUT) -> TradePlan:
    legs = [
        ContractLeg(symbol="INTC", action=OptionAction.BUY_TO_OPEN, option_type=otype,
                    strike=strikes[0], expiration=_EXP, quantity=1, entry_price=1.85),
        ContractLeg(symbol="INTC", action=OptionAction.SELL_TO_OPEN, option_type=otype,
                    strike=strikes[1], expiration=_EXP, quantity=1, entry_price=0.78),
    ]
    return TradePlan(
        symbol="INTC", direction=Direction.BEARISH, strategy=StrategyType.BEAR_PUT_SPREAD,
        legs=legs, net_debit=net_debit, contracts=contracts,
        risk=RiskPlan(max_loss_usd=abs(net_debit) * contracts, account_risk_pct=0.05,
                      profit_target_pct=target, stop_loss_pct=stop),
    )


# --- The pair, priced off the debit -------------------------------------------
def test_the_target_is_a_fraction_of_the_debit_not_of_max_profit() -> None:
    # THE regression. INTC: 1.07 debit, 4.00 wide. 50% of the debit is 1.605 and
    # would have filled; 50% of max profit is 2.535 and did not.
    b = bracket_from_entry(1.07, 1, width=4.0)
    assert b.target_price == 1.60          # 1.605 rounded toward an easier fill
    assert b.scale_out_price == pytest.approx(2.54, abs=0.01)
    assert b.target_price < b.scale_out_price


def test_stop_and_target_are_symmetric_about_the_entry() -> None:
    b = bracket_from_entry(1.07, 1)
    # Both round toward acting sooner: the target down, the stop up.
    assert (b.target_price, b.stop_price) == (1.60, 0.54)
    # Dollars are derived from the POSTED prices, not the ideal ones.
    assert b.target_pnl_usd == pytest.approx(53.0)
    assert b.stop_pnl_usd == pytest.approx(-53.0)


def test_pnl_scales_with_contracts_but_prices_do_not() -> None:
    one, three = bracket_from_entry(2.0, 1), bracket_from_entry(2.0, 3)
    assert one.target_price == three.target_price  # a net price is per-share
    assert three.target_pnl_usd == pytest.approx(one.target_pnl_usd * 3)


# --- Credit structures close the other way ------------------------------------
def test_a_credit_structure_profits_by_buying_back_cheaper() -> None:
    b = bracket_from_entry(-2.00, 1)  # opened for a $2.00 credit
    assert b.is_credit is True
    assert b.close_action == "buy_to_close"
    assert b.target_price == pytest.approx(1.00)   # pay less than you took in
    assert b.stop_price == pytest.approx(3.00)     # pay more = the loss
    assert b.target_pnl_usd > 0 > b.stop_pnl_usd


def test_a_credit_bracket_offers_no_scale_out_number() -> None:
    # "% of max profit" is a debit-vertical idea; inventing one for a credit
    # would be a different trade dressed as the same one.
    assert bracket_from_entry(-2.00, 1, width=5.0).scale_out_price is None


# --- Reading a plan -----------------------------------------------------------
def test_a_plan_yields_the_pair_using_its_own_exit_percentages() -> None:
    b = bracket_from_plan(_plan(107.0))  # net_debit is per-1-lot, in dollars
    assert b is not None
    assert b.entry_net == pytest.approx(1.07)
    assert (b.target_price, b.stop_price) == (1.60, 0.54)
    assert b.close_action == "sell_to_close"


def test_a_regime_specific_target_is_honoured_over_the_default() -> None:
    # Gamma regime takes 40%, not 50% — the bracket must follow the plan.
    b = bracket_from_plan(_plan(300.0, target=0.4, stop=0.5))
    assert b.target_price == pytest.approx(4.20, abs=0.01)
    assert b.stop_price == pytest.approx(1.50, abs=0.01)


def test_a_zero_percent_rule_falls_back_rather_than_pricing_a_no_op() -> None:
    # A recorded 0.0 means "no rule captured", not "exit at entry".
    b = bracket_from_plan(_plan(200.0, target=0.0, stop=0.0))
    assert b.target_pct == DEFAULT_TARGET_PCT
    assert b.stop_pct == DEFAULT_STOP_PCT


def test_width_is_only_inferred_from_a_genuine_vertical() -> None:
    # Mixed option types are not a vertical, so no max-profit number is invented.
    plan = _plan(107.0)
    plan.legs[1].option_type = OptionType.CALL
    assert bracket_from_plan(plan).scale_out_price is None


# --- Refusals -----------------------------------------------------------------
def test_a_zero_entry_has_no_bracket_to_price() -> None:
    assert bracket_from_entry(0.0, 1) is None


def test_the_stop_never_prices_below_a_tradeable_tick() -> None:
    b = bracket_from_entry(0.01, 1, stop_pct=0.9)
    assert b.stop_price >= 0.01


# --- What the human actually reads --------------------------------------------
def test_the_paste_line_names_the_action_and_both_prices() -> None:
    line = bracket_from_entry(1.07, 2, width=4.0).paste
    assert line.startswith("SELL to close x2")
    assert "TARGET 1.60" in line and "STOP 0.54" in line
    assert "scale-out 2.54" in line


def test_the_note_carries_the_gap_caveat_with_the_number() -> None:
    # The INTC loss came through an overnight gap, so the caveat must travel with
    # the stop rather than live in docs read only after the fact.
    assert "not gap protection" in bracket_from_entry(1.07, 1).note
