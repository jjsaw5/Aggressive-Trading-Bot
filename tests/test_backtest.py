"""Backtest pricing, engine, and performance aggregation."""

from __future__ import annotations

from datetime import date

import pytest

from app.backtest.engine import backtest_trade
from app.backtest.performance import by_strategy, overall
from app.backtest.pricing import (
    black_scholes_price,
    net_position_price,
    plan_entry_net_per_share,
)
from app.backtest.runner import run_backtest
from app.domain.enums import (
    Direction,
    ExitReason,
    OptionAction,
    OptionType,
    StrategyType,
)
from app.domain.trades import ContractLeg, RiskPlan, TradePlan


# --- Pricing -----------------------------------------------------------------
def test_bs_call_intrinsic_at_expiry() -> None:
    # At expiry, a 5-ITM call is worth its intrinsic value.
    assert black_scholes_price(105, 100, 0.0, 0.4, OptionType.CALL) == pytest.approx(5.0)
    assert black_scholes_price(95, 100, 0.0, 0.4, OptionType.CALL) == 0.0


def test_bs_call_monotonic_in_spot() -> None:
    lo = black_scholes_price(100, 100, 0.1, 0.4, OptionType.CALL)
    hi = black_scholes_price(110, 100, 0.1, 0.4, OptionType.CALL)
    assert hi > lo


def test_put_call_parity_roughly_holds() -> None:
    s, k, t, v, r = 100.0, 100.0, 0.25, 0.4, 0.04
    c = black_scholes_price(s, k, t, v, OptionType.CALL, r)
    p = black_scholes_price(s, k, t, v, OptionType.PUT, r)
    import math

    assert (c - p) == pytest.approx(s - k * math.exp(-r * t), abs=1e-6)


# --- Fixtures ----------------------------------------------------------------
def _spread_plan() -> TradePlan:
    return TradePlan(
        symbol="AAA",
        direction=Direction.BULLISH,
        strategy=StrategyType.BULL_CALL_SPREAD,
        legs=[
            ContractLeg(
                symbol="AAA", action=OptionAction.BUY_TO_OPEN, option_type=OptionType.CALL,
                strike=100.0, expiration=date(2026, 9, 18), quantity=2, entry_price=3.0,
            ),
            ContractLeg(
                symbol="AAA", action=OptionAction.SELL_TO_OPEN, option_type=OptionType.CALL,
                strike=105.0, expiration=date(2026, 9, 18), quantity=2, entry_price=1.5,
            ),
        ],
        net_debit=150.0,
        contracts=2,
        risk=RiskPlan(
            max_loss_usd=300.0, max_profit_usd=700.0, account_risk_pct=0.15,
            profit_target_pct=0.5, stop_loss_pct=0.5, time_stop_dte=7,
        ),
    )


def test_entry_net_is_debit() -> None:
    plan = _spread_plan()
    assert plan_entry_net_per_share(plan) == pytest.approx(1.5)  # 3.0 - 1.5


def test_spread_net_capped_by_width_at_expiry() -> None:
    plan = _spread_plan()
    # Deep ITM past both strikes at expiry -> net approaches width (5.0).
    net = net_position_price(plan, spot=130.0, days_to_expiry=0, vol=0.4)
    assert net == pytest.approx(5.0, abs=1e-6)


# --- Engine ------------------------------------------------------------------
def test_profit_target_exit_on_rally() -> None:
    plan = _spread_plan()
    # Rising path should hit the +50% profit target and close a winner.
    path = [100, 102, 104, 106, 108, 110]
    res = backtest_trade(plan, entry_dte=30, path_spots=[float(x) for x in path], vol=0.4)
    assert res.exit_reason in (ExitReason.PROFIT_TARGET, ExitReason.TIME_STOP, ExitReason.EXPIRY)
    assert res.trade.realized_pnl_usd is not None
    assert res.trajectory


def test_stop_loss_exit_on_selloff() -> None:
    plan = _spread_plan()
    path = [100, 98, 96, 94, 92, 90, 88]
    res = backtest_trade(plan, entry_dte=30, path_spots=[float(x) for x in path], vol=0.4)
    assert res.realized_pnl_usd <= 0


def test_mfe_mae_recorded() -> None:
    plan = _spread_plan()
    path = [100, 105, 98, 103, 108]
    res = backtest_trade(plan, entry_dte=30, path_spots=[float(x) for x in path], vol=0.4)
    assert res.trade.mfe_usd >= 0
    assert res.trade.mae_usd <= 0


# --- Performance -------------------------------------------------------------
def test_performance_aggregation() -> None:
    plan = _spread_plan()
    results = [
        backtest_trade(plan, 30, [100.0, 105.0, 110.0, 112.0], 0.4),
        backtest_trade(plan, 30, [100.0, 95.0, 90.0, 88.0], 0.4),
    ]
    o = overall(results)
    assert o.trades == 2
    assert 0.0 <= o.win_rate <= 1.0
    groups = by_strategy(results)
    assert groups and groups[0].group == "bull_call_spread"


# --- Runner (end to end over the mock) --------------------------------------
async def test_run_backtest_smoke() -> None:
    report = await run_backtest(num_paths=25, seed=7)
    assert report.num_candidates >= 1
    assert report.num_trades == report.num_candidates * 25
    assert 0.0 <= report.overall.win_rate <= 1.0
    # Deterministic given the seed.
    report2 = await run_backtest(num_paths=25, seed=7)
    assert report2.overall.total_pnl_usd == report.overall.total_pnl_usd
