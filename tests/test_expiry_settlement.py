"""Settling a decision at its own expiry.

The gap this closes: thousands of recorded engine decisions were never graded,
so the conviction gate had nothing to score. Grading them against TODAY's price
would be worse than not grading — an April 0DTE call judged on July's tape is a
confident number about nothing. Each decision is settled at its own expiry.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.analytics.expiry_settlement import (
    settle_at_expiry,
    structure_value_at_expiry,
)
from app.domain.enums import (
    Direction,
    OptionAction,
    OptionType,
    StrategyType,
)
from app.domain.outcomes import DecisionSnapshot, OutcomeResult
from app.domain.trades import ContractLeg, RiskPlan, TradePlan

_EXP = date(2026, 7, 24)
_GEN = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
_NOW = datetime(2026, 7, 28, 20, 0, tzinfo=UTC)


def _leg(strike: float, otype: OptionType, *, long: bool) -> ContractLeg:
    return ContractLeg(
        symbol="TSLA",
        action=OptionAction.BUY_TO_OPEN if long else OptionAction.SELL_TO_OPEN,
        option_type=otype, strike=strike, expiration=_EXP, quantity=1, entry_price=1.0,
    )


def _plan(legs, *, net_per_share: float, contracts: int = 1,
          direction=Direction.BEARISH, strategy=StrategyType.BEAR_PUT_SPREAD) -> TradePlan:
    max_loss = abs(net_per_share) * 100 * contracts
    return TradePlan(
        symbol="TSLA", direction=direction, strategy=strategy, legs=legs,
        net_debit=round(net_per_share * 100, 2), contracts=contracts,
        risk=RiskPlan(max_loss_usd=max_loss, account_risk_pct=0.05,
                      profit_target_pct=0.5, stop_loss_pct=0.5),
    )


def _snap(plan: TradePlan, *, entry_spot: float = 370.0,
          direction=Direction.BEARISH) -> DecisionSnapshot:
    return DecisionSnapshot(
        decision_id="d1", scan_id="s1", symbol="TSLA", direction=direction,
        strategy=plan.strategy, generated_at=_GEN, composite_score=0.7,
        entry_spot=entry_spot, trade_plan=plan,
        entry_net_per_share=plan.net_debit / 100.0,
        max_loss_usd=plan.risk.max_loss_usd, contracts=plan.contracts,
        expiration=_EXP,
    )


# --- Settlement value is exact intrinsic --------------------------------------
def test_put_debit_spread_at_max_value_when_fully_in_the_money() -> None:
    # Long 370P / short 365P, underlying finishes at 360: both ITM, spread is
    # worth its full 5-point width.
    plan = _plan([_leg(370, OptionType.PUT, long=True),
                  _leg(365, OptionType.PUT, long=False)], net_per_share=2.45)
    assert structure_value_at_expiry(plan, 360.0) == 5.0


def test_spread_is_worthless_when_both_legs_expire_out_of_the_money() -> None:
    plan = _plan([_leg(370, OptionType.PUT, long=True),
                  _leg(365, OptionType.PUT, long=False)], net_per_share=2.45)
    assert structure_value_at_expiry(plan, 380.0) == 0.0


def test_partial_value_between_the_strikes() -> None:
    plan = _plan([_leg(370, OptionType.PUT, long=True),
                  _leg(365, OptionType.PUT, long=False)], net_per_share=2.45)
    assert structure_value_at_expiry(plan, 367.5) == 2.5  # long ITM by 2.5, short OTM


def test_short_legs_subtract_their_intrinsic() -> None:
    # A credit call spread: short the 100C, long the 110C. At 105 the short leg
    # is 5 in the money against you and the long is worthless.
    plan = _plan([_leg(100, OptionType.CALL, long=False),
                  _leg(110, OptionType.CALL, long=True)], net_per_share=-1.50)
    assert structure_value_at_expiry(plan, 105.0) == -5.0
    assert structure_value_at_expiry(plan, 95.0) == 0.0    # both expire worthless
    assert structure_value_at_expiry(plan, 130.0) == -10.0  # capped at the width


def test_long_single_leg() -> None:
    plan = _plan([_leg(370, OptionType.PUT, long=True)], net_per_share=3.00)
    assert structure_value_at_expiry(plan, 350.0) == 20.0
    assert structure_value_at_expiry(plan, 400.0) == 0.0


# --- Grading ------------------------------------------------------------------
def test_a_winning_decision_is_graded_win_with_costs_netted() -> None:
    plan = _plan([_leg(370, OptionType.PUT, long=True),
                  _leg(365, OptionType.PUT, long=False)], net_per_share=2.45)
    o = settle_at_expiry(_snap(plan), 360.0, resolved_at=_NOW)
    assert o is not None
    assert o.result == OutcomeResult.WIN
    assert o.realized_pnl_gross_usd == pytest.approx((5.0 - 2.45) * 100)  # +255
    assert o.costs_usd > 0 and o.realized_pnl_usd < o.realized_pnl_gross_usd
    assert o.horizon_label == "at_expiry"
    assert o.outcome_source == "expiry_settlement"
    # Settlement is exact intrinsic — nothing was modelled.
    assert o.used_bs_fallback is False


def test_a_total_loss_is_graded_loss() -> None:
    plan = _plan([_leg(370, OptionType.PUT, long=True),
                  _leg(365, OptionType.PUT, long=False)], net_per_share=2.45)
    o = settle_at_expiry(_snap(plan), 380.0, resolved_at=_NOW)
    assert o is not None and o.result == OutcomeResult.LOSS
    assert o.realized_pnl_gross_usd == pytest.approx(-245.0)


def test_roughly_flat_is_a_scratch_not_a_win() -> None:
    # Settles at 2.45 against a 2.45 entry: zero gross. Scaled to the position's
    # own risk, that's noise — grading it a win/loss would be dishonest.
    plan = _plan([_leg(370, OptionType.PUT, long=True),
                  _leg(365, OptionType.PUT, long=False)], net_per_share=2.45)
    o = settle_at_expiry(_snap(plan), 367.55, resolved_at=_NOW, include_costs=False)
    assert o is not None and o.result == OutcomeResult.SCRATCH


def test_elapsed_days_run_to_the_expiry_not_to_today() -> None:
    # The decision was made 7/20 for a 7/24 expiry and is being settled on 7/28:
    # its horizon is 4 days, not 8.
    plan = _plan([_leg(370, OptionType.PUT, long=True),
                  _leg(365, OptionType.PUT, long=False)], net_per_share=2.45)
    o = settle_at_expiry(_snap(plan), 360.0, resolved_at=_NOW)
    assert o is not None and o.elapsed_days == 4


def test_direction_is_graded_against_the_underlying_move() -> None:
    plan = _plan([_leg(370, OptionType.PUT, long=True),
                  _leg(365, OptionType.PUT, long=False)], net_per_share=2.45)
    snap = _snap(plan, entry_spot=370.0, direction=Direction.BEARISH)
    assert settle_at_expiry(snap, 360.0, resolved_at=_NOW).direction_correct is True
    assert settle_at_expiry(snap, 380.0, resolved_at=_NOW).direction_correct is False


def test_a_non_directional_decision_gets_no_direction_grade() -> None:
    plan = _plan([_leg(370, OptionType.PUT, long=True)], net_per_share=3.0,
                 direction=Direction.NEUTRAL, strategy=StrategyType.LONG_STRADDLE)
    o = settle_at_expiry(_snap(plan, direction=Direction.NEUTRAL), 360.0, resolved_at=_NOW)
    assert o is not None and o.direction_correct is None


def test_pnl_scales_with_contracts() -> None:
    legs = [_leg(370, OptionType.PUT, long=True), _leg(365, OptionType.PUT, long=False)]
    one = settle_at_expiry(_snap(_plan(legs, net_per_share=2.45)), 360.0,
                           resolved_at=_NOW, include_costs=False)
    three = settle_at_expiry(_snap(_plan(legs, net_per_share=2.45, contracts=3)), 360.0,
                             resolved_at=_NOW, include_costs=False)
    assert three.realized_pnl_usd == pytest.approx(one.realized_pnl_usd * 3)


def test_a_snapshot_without_legs_cannot_be_settled() -> None:
    # Abstain rather than invent a payoff for a record we can't price.
    plan = _plan([], net_per_share=3.0)
    assert settle_at_expiry(_snap(plan), 360.0, resolved_at=_NOW) is None


def test_the_note_states_the_policy_being_graded() -> None:
    # Hold-to-expiry is NOT the app's managed exit plan; pooling the two would
    # misrepresent both.
    plan = _plan([_leg(370, OptionType.PUT, long=True),
                  _leg(365, OptionType.PUT, long=False)], net_per_share=2.45)
    o = settle_at_expiry(_snap(plan), 360.0, resolved_at=_NOW)
    assert "Hold-to-expiry" in o.note and "NOT the managed" in o.note
