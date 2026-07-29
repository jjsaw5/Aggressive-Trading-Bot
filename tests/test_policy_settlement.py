"""Grading a decision under the exit policy the app actually runs.

Expiry settlement grades a hold-to-expiry payoff — correct for POP, wrong for
"would the strategy have made money", because the app takes profit at 40-60%,
stops at -50%, and time-stops by DTE regime. Every one of the 127 graded
outcomes was hold-to-expiry, so P&L discrimination was measuring a policy
nobody runs.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.analytics.policy_settlement import (
    occ_symbol,
    settle_under_policy,
    walk_policy,
)
from app.domain.enums import (
    Direction,
    OptionAction,
    OptionType,
    StrategyType,
)
from app.domain.outcomes import DecisionSnapshot, OutcomeResult
from app.domain.trades import ContractLeg, RiskPlan, TradePlan

_ENTRY = date(2026, 7, 6)
_EXP = date(2026, 7, 24)
_GEN = datetime(2026, 7, 6, 14, 30, tzinfo=UTC)
_NOW = datetime(2026, 7, 28, 20, 0, tzinfo=UTC)

_LONG = occ_symbol("TSLA", _EXP, OptionType.PUT, 370.0)
_SHORT = occ_symbol("TSLA", _EXP, OptionType.PUT, 365.0)


def _plan(*, target=0.5, stop=0.5, time_stop=None, contracts=1) -> TradePlan:
    legs = [
        ContractLeg(symbol="TSLA", action=OptionAction.BUY_TO_OPEN,
                    option_type=OptionType.PUT, strike=370.0, expiration=_EXP,
                    quantity=1, entry_price=3.05),
        ContractLeg(symbol="TSLA", action=OptionAction.SELL_TO_OPEN,
                    option_type=OptionType.PUT, strike=365.0, expiration=_EXP,
                    quantity=1, entry_price=0.60),
    ]
    return TradePlan(
        symbol="TSLA", direction=Direction.BEARISH, strategy=StrategyType.BEAR_PUT_SPREAD,
        legs=legs, net_debit=245.0, contracts=contracts,
        risk=RiskPlan(max_loss_usd=245.0 * contracts, account_risk_pct=0.05,
                      profit_target_pct=target, stop_loss_pct=stop,
                      time_stop_dte=time_stop),
    )


def _snap(plan: TradePlan) -> DecisionSnapshot:
    return DecisionSnapshot(
        decision_id="d1", scan_id="s1", symbol="TSLA", direction=Direction.BEARISH,
        strategy=plan.strategy, generated_at=_GEN, composite_score=0.7,
        entry_spot=370.0, trade_plan=plan, entry_net_per_share=2.45,
        max_loss_usd=plan.risk.max_loss_usd, contracts=plan.contracts,
        expiration=_EXP,
    )


def _marks(days: dict[date, tuple[float, float]]) -> dict[date, dict[str, float]]:
    """{date: (long_mark, short_mark)} -> the nested shape the walker takes."""
    return {d: {_LONG: lo, _SHORT: sh} for d, (lo, sh) in days.items()}


def test_occ_symbol_matches_the_feeds_contract_format() -> None:
    assert occ_symbol("TSLA", date(2026, 7, 24), OptionType.PUT, 370.0) == "TSLA260724P00370000"
    assert occ_symbol("spy", date(2026, 8, 21), OptionType.CALL, 645.5) == "SPY260821C00645500"


# --- Exit rules fire in the right order ---------------------------------------
def test_profit_target_exits_the_day_it_is_reached() -> None:
    # Entry 2.45. A 50% target means a net of 3.675.
    marks = _marks({
        date(2026, 7, 7): (3.20, 0.65),   # 2.55 — +4%, hold
        date(2026, 7, 8): (4.60, 0.85),   # 3.75 — +53%, take it
        date(2026, 7, 9): (5.00, 0.90),   # would have been better; irrelevant
    })
    ex = walk_policy(_plan(), 2.45, _ENTRY, _EXP, marks, underlying_close_at_expiry=360.0)
    assert ex is not None
    assert ex.reason == "profit_target" and ex.exit_date == date(2026, 7, 8)


def test_stop_loss_exits_and_is_checked_before_the_target() -> None:
    # A day that traded through BOTH must book the loss — assuming the good fill
    # on an ambiguous bar is how a backtest flatters itself.
    marks = _marks({date(2026, 7, 8): (1.30, 0.10)})  # 1.20 -> -51%
    ex = walk_policy(_plan(), 2.45, _ENTRY, _EXP, marks, underlying_close_at_expiry=380.0)
    assert ex is not None and ex.reason == "stop_loss"


def test_time_stop_fires_on_dte_remaining() -> None:
    marks = _marks({
        date(2026, 7, 15): (3.00, 0.70),  # 2.30, nothing triggered
        date(2026, 7, 20): (3.10, 0.75),  # 2.35, 4 DTE left -> time stop
    })
    ex = walk_policy(_plan(time_stop=7), 2.45, _ENTRY, _EXP, marks,
                     underlying_close_at_expiry=372.0)
    assert ex is not None
    assert ex.reason == "time_stop" and ex.exit_date == date(2026, 7, 20)


def test_nothing_triggering_falls_through_to_expiry_intrinsic() -> None:
    marks = _marks({date(2026, 7, 15): (3.00, 0.70)})  # 2.30, no trigger
    ex = walk_policy(_plan(), 2.45, _ENTRY, _EXP, marks, underlying_close_at_expiry=360.0)
    assert ex is not None
    assert ex.reason == "expiry" and ex.exit_net == 5.0  # both legs ITM, full width


def test_an_unpriced_day_is_held_through_not_invented() -> None:
    marks = {
        date(2026, 7, 8): {_LONG: 4.60},  # short leg missing -> cannot value
        date(2026, 7, 9): {_LONG: 4.60, _SHORT: 0.85},  # 3.75 -> target
    }
    ex = walk_policy(_plan(), 2.45, _ENTRY, _EXP, marks, underlying_close_at_expiry=360.0)
    assert ex is not None and ex.exit_date == date(2026, 7, 9)


def test_marks_before_entry_are_ignored() -> None:
    # Look-ahead in reverse: a pre-entry bar must never trigger an exit.
    marks = _marks({date(2026, 7, 1): (1.00, 0.05), date(2026, 7, 15): (3.00, 0.70)})
    ex = walk_policy(_plan(), 2.45, _ENTRY, _EXP, marks, underlying_close_at_expiry=372.0)
    assert ex is not None and ex.reason == "expiry"


# --- Grading ------------------------------------------------------------------
def test_managed_win_is_graded_and_labeled_as_the_real_policy() -> None:
    marks = _marks({date(2026, 7, 8): (4.60, 0.85)})
    o = settle_under_policy(_snap(_plan()), marks, resolved_at=_NOW,
                            underlying_close_at_expiry=360.0)
    assert o is not None
    assert o.result == OutcomeResult.WIN
    assert o.realized_pnl_gross_usd == pytest.approx((3.75 - 2.45) * 100)
    assert o.outcome_source == "managed_policy"
    assert o.horizon_label == "managed_exit"
    assert "This is the policy the app runs" in o.note
    assert o.elapsed_days == 2  # entry 7/6 -> exit 7/8, not to expiry


def test_the_managed_grade_can_differ_from_hold_to_expiry() -> None:
    # The whole reason this module exists: same decision, opposite verdicts.
    # Takes profit at +53% on 7/8, then the underlying reverses and the
    # structure would have expired worthless.
    from app.analytics.expiry_settlement import settle_at_expiry

    snap = _snap(_plan())
    marks = _marks({date(2026, 7, 8): (4.60, 0.85)})
    managed = settle_under_policy(snap, marks, resolved_at=_NOW,
                                  underlying_close_at_expiry=390.0)
    held = settle_at_expiry(snap, 390.0, resolved_at=_NOW)
    assert managed.result == OutcomeResult.WIN
    assert held.result == OutcomeResult.LOSS


def test_an_unpriceable_decision_abstains_rather_than_modelling_a_path() -> None:
    # No marks at all and no expiry close: refuse to grade.
    assert settle_under_policy(_snap(_plan()), {}, resolved_at=_NOW) is None


def test_the_exit_reason_is_a_field_not_buried_in_prose() -> None:
    marks = _marks({date(2026, 7, 8): (1.30, 0.10)})
    o = settle_under_policy(_snap(_plan()), marks, resolved_at=_NOW,
                            underlying_close_at_expiry=380.0)
    assert o.exit_reason == "stop_loss"


# --- The scorecard must spend the managed dollars, not the held ones ----------
def test_the_scorecard_prefers_the_managed_grade_for_pnl() -> None:
    from app.analytics.calibration import select_pnl_outcomes, select_scoring_outcomes

    marks = _marks({date(2026, 7, 8): (4.60, 0.85)})
    snap = _snap(_plan())
    managed = settle_under_policy(snap, marks, resolved_at=_NOW,
                                  underlying_close_at_expiry=390.0)
    from app.analytics.expiry_settlement import settle_at_expiry

    held = settle_at_expiry(snap, 390.0, resolved_at=_NOW)
    both = [held, managed]

    # P&L takes the managed replay; win rate / Brier keep the hold-to-expiry
    # grade, because probability-of-profit is a hold-to-expiry claim.
    assert select_pnl_outcomes(both)["d1"].outcome_source == "managed_policy"
    assert select_scoring_outcomes(both)["d1"].outcome_source == "expiry_settlement"


def test_pnl_selection_ignores_outcomes_carrying_no_dollars() -> None:
    from app.analytics.calibration import select_pnl_outcomes
    from app.domain.outcomes import DecisionOutcome

    proxy = DecisionOutcome(decision_id="d1", symbol="TSLA", horizon_label="21d",
                            resolved_at=_NOW, result=OutcomeResult.WIN,
                            outcome_source="underlying_vs_breakeven")
    assert select_pnl_outcomes([proxy]) == {}


def test_pnl_scales_with_contracts() -> None:
    marks = _marks({date(2026, 7, 8): (4.60, 0.85)})
    one = settle_under_policy(_snap(_plan()), marks, resolved_at=_NOW,
                              underlying_close_at_expiry=360.0, include_costs=False)
    three = settle_under_policy(_snap(_plan(contracts=3)), marks, resolved_at=_NOW,
                                underlying_close_at_expiry=360.0, include_costs=False)
    assert three.realized_pnl_usd == pytest.approx(one.realized_pnl_usd * 3)
