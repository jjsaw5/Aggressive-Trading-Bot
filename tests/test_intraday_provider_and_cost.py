"""Parsing UW minute bars, and the cost figures the capital gate depends on.

The parser fixtures below are verbatim-shaped rows from the live response for
`SPY260728C00730000` on 2026-07-28, so a schema drift at UW breaks a test here
rather than silently producing a wrong grade.

The cost-stress assertions matter more than they look. `CAPTURE_WINDOW_
PREREGISTRATION.md` §6 makes H4-after-one-tick-stress a precondition for live
capital, so an error in this arithmetic is an error in the gate that decides
whether real money moves.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.analytics.cost_stress import (
    SOURCE_EFFECTIVE,
    effective_half_spread,
    stress_pnl,
)
from app.analytics.slippage import FillComparison, compare_fills
from app.domain.enums import Direction, OptionAction, OptionType, StrategyType
from app.domain.trades import ContractLeg, RiskPlan, TradePlan
from app.providers.unusual_whales.intraday import parse_minute_bar

_EXP = date(2026, 8, 21)

# Shape taken from the live UW response.
_ROW = {
    "close": "10.73", "high": "10.88", "low": "10.73", "open": "10.88",
    "start_time": "2026-07-28T20:00:00Z", "avg_price": "10.83",
    "iv_high": "13.8953", "iv_low": "0.4489",
    "premium_ask_side": 33584, "premium_bid_side": 27082,
    "premium_mid_side": 0, "premium_no_side": 0,
    "volume_ask_side": 31, "volume_bid_side": 25,
    "volume_mid_side": 0, "volume_no_side": 0,
}


def _plan(legs: int = 2) -> TradePlan:
    ls = [
        ContractLeg(symbol="SPY", action=OptionAction.BUY_TO_OPEN,
                    option_type=OptionType.CALL, strike=500.0, expiration=_EXP,
                    quantity=1, entry_price=3.0),
    ]
    if legs == 2:
        ls.append(ContractLeg(symbol="SPY", action=OptionAction.SELL_TO_OPEN,
                              option_type=OptionType.CALL, strike=505.0, expiration=_EXP,
                              quantity=1, entry_price=2.0))
    return TradePlan(
        symbol="SPY", direction=Direction.BULLISH, strategy=StrategyType.BULL_CALL_SPREAD,
        legs=ls, net_debit=100.0, contracts=1,
        risk=RiskPlan(max_loss_usd=100.0, account_risk_pct=0.05,
                      profit_target_pct=0.5, stop_loss_pct=0.5),
    )


# --- Parser -------------------------------------------------------------------
def test_a_live_shaped_row_parses() -> None:
    b = parse_minute_bar(_ROW, "SPY260728C00730000")
    assert b is not None
    assert (b.open, b.high, b.low, b.close) == (10.88, 10.88, 10.73, 10.73)
    assert b.volume == 56  # 31 ask + 25 bid + 0 mid


def test_numeric_strings_and_numbers_both_parse() -> None:
    # UW mixes quoted decimals and bare integers in the same object.
    b = parse_minute_bar(_ROW, "X")
    assert b.premium_ask_side == 33584.0 and b.iv_high == pytest.approx(13.8953)


def test_effective_bid_ask_come_from_real_executions() -> None:
    b = parse_minute_bar(_ROW, "X")
    assert b.effective_ask == pytest.approx(33584 / (31 * 100), abs=1e-4)
    assert b.effective_bid == pytest.approx(27082 / (25 * 100), abs=1e-4)
    assert b.effective_spread == pytest.approx(b.effective_ask - b.effective_bid)


def test_a_one_sided_minute_cannot_price_a_spread() -> None:
    # Half a spread is not an estimate of the whole one.
    row = {**_ROW, "volume_bid_side": 0, "premium_bid_side": 0}
    b = parse_minute_bar(row, "X")
    assert b.effective_bid is None and b.effective_spread is None
    assert b.effective_ask is not None  # the side that DID trade still reports


def test_a_row_missing_any_ohlc_is_dropped_not_backfilled() -> None:
    for missing in ("open", "high", "low", "close"):
        assert parse_minute_bar({**_ROW, missing: None}, "X") is None


def test_a_crossed_bar_is_refused() -> None:
    assert parse_minute_bar({**_ROW, "high": "1.0", "low": "9.0"}, "X") is None


def test_a_row_without_a_timestamp_is_dropped() -> None:
    assert parse_minute_bar({**_ROW, "start_time": None}, "X") is None


def test_absent_volume_is_zero_but_absent_price_is_not() -> None:
    # Zero contracts traded is a fact; a price of zero is not.
    b = parse_minute_bar({k: v for k, v in _ROW.items() if k != "volume_mid_side"}, "X")
    assert b.volume_mid_side == 0
    assert b.avg_price is not None


# --- Cost stress (item 2.4) ---------------------------------------------------
def test_one_tick_is_charged_on_every_leg_in_both_directions() -> None:
    # 2 legs x 2 directions x $0.01 x 100 x 1 contract = $4.00
    s = stress_pnl(_plan(), pnl_mid_usd=50.0, contracts=1)
    assert s.pnl_1tick_usd == pytest.approx(46.0)
    assert s.tick_drag_usd == pytest.approx(4.0)


def test_the_tick_drag_scales_with_legs_and_contracts() -> None:
    assert stress_pnl(_plan(legs=1), 50.0, 1).tick_drag_usd == pytest.approx(2.0)
    assert stress_pnl(_plan(), 50.0, 3).tick_drag_usd == pytest.approx(12.0)


def test_tick_drag_is_material_against_a_100_dollar_risk_cap() -> None:
    # The reason this is the headline figure and not a footnote: $4 on a $100
    # defined-risk trade is 4% of max loss, before any spread.
    assert stress_pnl(_plan(), 50.0, 1).tick_drag_usd / 100.0 == pytest.approx(0.04)


def test_a_winner_can_be_erased_by_the_stress() -> None:
    s = stress_pnl(_plan(), pnl_mid_usd=3.0, contracts=1)
    assert s.pnl_mid_usd > 0 and s.pnl_1tick_usd < 0


def test_half_spread_is_absent_rather_than_guessed() -> None:
    assert stress_pnl(_plan(), 50.0, 1).pnl_half_spread_usd is None


def test_half_spread_is_charged_per_leg_both_ways_when_known() -> None:
    s = stress_pnl(_plan(), 50.0, 1, half_spread_per_leg=0.05)
    assert s.pnl_half_spread_usd == pytest.approx(50.0 - (0.05 * 2 * 2 * 100))


def test_every_stress_carries_its_source() -> None:
    # An execution-derived spread and a quoted NBBO are not interchangeable.
    assert stress_pnl(_plan(), 50.0, 1).source == SOURCE_EFFECTIVE


def test_the_half_spread_is_a_median_not_a_mean() -> None:
    class _B:
        def __init__(self, s): self.effective_spread = s

    # One 10.0 outlier must not drag the estimate; median of the spreads is 0.20.
    bars = [_B(0.20), _B(0.20), _B(0.20), _B(10.0)]
    assert effective_half_spread(bars) == pytest.approx(0.10)


def test_no_two_sided_minute_means_no_half_spread() -> None:
    class _B:
        effective_spread = None

    assert effective_half_spread([_B(), _B()]) is None


# --- Slippage (item 2.5) ------------------------------------------------------
def _cmp(modeled: float, actual: float, legs: int = 2) -> FillComparison:
    return FillComparison("sd:x", "SPY", modeled, actual, 1, legs)


def test_paying_more_than_modeled_on_a_debit_is_positive_cost() -> None:
    assert _cmp(1.00, 1.04).slippage_per_share == pytest.approx(0.04)


def test_receiving_less_than_modeled_on_a_credit_is_also_positive_cost() -> None:
    # Credit net is negative; receiving less moves it toward zero. Same formula.
    assert _cmp(-1.00, -0.96).slippage_per_share == pytest.approx(0.04)


def test_a_better_than_modeled_fill_is_negative_cost() -> None:
    assert _cmp(1.00, 0.98).slippage_per_share == pytest.approx(-0.02)


def test_slippage_converts_to_ticks_per_leg_for_comparison() -> None:
    # This is the unit that validates or refutes the one-tick stress.
    assert _cmp(1.00, 1.04, legs=2).slippage_ticks_per_leg == pytest.approx(2.0)


def test_a_thin_sample_refuses_to_render_a_verdict() -> None:
    r = compare_fills([_cmp(1.00, 1.04) for _ in range(9)])
    assert "UNDETERMINED" in r.verdict and r.n == 9


def test_a_sufficient_sample_calls_the_one_tick_assumption() -> None:
    r = compare_fills([_cmp(1.00, 1.04) for _ in range(12)])
    assert "UNDERSTATES" in r.verdict
    assert r.median_ticks_per_leg == pytest.approx(2.0)


def test_slippage_within_one_tick_validates_the_assumption() -> None:
    r = compare_fills([_cmp(1.00, 1.01) for _ in range(12)])
    assert "ADEQUATE" in r.verdict


def test_an_empty_population_is_not_an_answer() -> None:
    r = compare_fills([])
    assert r.n == 0 and r.median_per_share is None and "UNDETERMINED" in r.verdict
