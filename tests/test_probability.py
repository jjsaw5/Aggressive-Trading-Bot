"""Market-implied probability of profit + the plain-English what-has-to-happen line."""

from __future__ import annotations

from app.quant.probability import (
    move_to_breakeven_pct,
    prob_finish_above,
    probability_of_profit,
    what_has_to_happen,
)


def test_at_the_money_is_roughly_even() -> None:
    # Break-even at spot, zero drift -> the small -0.5*sigma^2 term pulls it a hair
    # under 50%, but it stays close.
    p = prob_finish_above(spot=100, target=100, iv=0.4, days=30)
    assert p is not None
    assert 0.40 <= p <= 0.50


def test_far_otm_target_is_unlikely() -> None:
    # A target 30% above spot with 30 days and 40% vol is a long shot.
    p = prob_finish_above(spot=100, target=130, iv=0.4, days=30)
    assert p is not None and p < 0.15


def test_bearish_pop_is_complement_of_bullish() -> None:
    # Same break-even: bearish profit-below is 1 - bullish profit-above.
    bull = probability_of_profit(spot=100, breakeven=105, iv=0.4, days=30, bullish=True)
    bear = probability_of_profit(spot=100, breakeven=105, iv=0.4, days=30, bullish=False)
    assert bull is not None and bear is not None
    assert abs((bull + bear) - 1.0) < 1e-6


def test_the_tsla_case_is_a_coin_flip_at_best() -> None:
    # TSLA ~$379, bearish spread break-even ~$367.55, ~3 days, ~55% IV: a low-odds,
    # capped-payoff bet — the number a human should see before holding into earnings.
    p = probability_of_profit(spot=378.89, breakeven=367.55, iv=0.55, days=3, bullish=False)
    assert p is not None and p < 0.45


def test_degenerate_inputs_return_none() -> None:
    assert prob_finish_above(spot=0, target=100, iv=0.4, days=30) is None
    assert prob_finish_above(spot=100, target=100, iv=0.0, days=30) is None
    assert prob_finish_above(spot=100, target=100, iv=0.4, days=0) is None
    assert probability_of_profit(spot=-1, breakeven=100, iv=0.4, days=30, bullish=True) is None


def test_move_to_breakeven_sign() -> None:
    assert move_to_breakeven_pct(100, 105) == 5.0
    assert move_to_breakeven_pct(100, 96) == -4.0


def test_what_has_to_happen_reads_plainly() -> None:
    line = what_has_to_happen(symbol="TSLA", spot=378.89, breakeven=367.55, days=3, bullish=False)
    assert "TSLA" in line and "fall" in line and "367.55" in line and "(3d)" in line


def test_what_has_to_happen_already_past_breakeven() -> None:
    # Bullish trade already above its break-even -> "already past" phrasing.
    line = what_has_to_happen(symbol="AAPL", spot=200, breakeven=190, days=5, bullish=True)
    assert "already past" in line and "above" in line


def test_what_has_to_happen_empty_on_bad_input() -> None:
    assert what_has_to_happen(symbol="X", spot=0, breakeven=100, days=3, bullish=True) == ""
