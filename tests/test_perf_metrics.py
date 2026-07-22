"""Phase 2 shared performance primitives: drawdown, profit factor, expectancy."""

from __future__ import annotations

from app.analytics.metrics import expectancy, max_drawdown, profit_factor


def test_max_drawdown_peak_to_trough() -> None:
    # cumulative curve: 100, 40, 140, 60 -> largest peak-to-trough is 140->60 = 80.
    assert max_drawdown([100, -60, 100, -80]) == 80.0


def test_max_drawdown_all_up_is_zero() -> None:
    assert max_drawdown([10, 20, 5]) == 0.0


def test_max_drawdown_empty() -> None:
    assert max_drawdown([]) == 0.0


def test_max_drawdown_order_matters() -> None:
    # Same P&L multiset, different order -> different drawdown (why order is required).
    assert max_drawdown([-80, 100, 100, -60]) == 80.0
    assert max_drawdown([100, 100, -60, -80]) == 140.0


def test_profit_factor_and_none_without_losses() -> None:
    assert profit_factor([100, -50, 50]) == 3.0  # 150 / 50
    assert profit_factor([100, 50]) is None  # no losses -> undefined, not infinite


def test_expectancy() -> None:
    assert expectancy([100, -50, 40]) == 30.0
    assert expectancy([]) is None
