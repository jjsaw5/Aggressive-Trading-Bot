"""Credit-aware paper/backtest exits for credit spreads and iron condors."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.backtest.engine import backtest_trade
from app.domain.enums import Direction, ExitReason, OptionType
from app.domain.options import Greeks, OptionChain, OptionContract
from app.engine.contract_selection import select_credit_vertical, select_iron_condor
from app.quant.pricing import black_scholes_delta, black_scholes_price
from app.risk.policy import RiskPolicy
from app.risk.trade_plan import build_structure_plan
from app.services.paper_engine import SlippageModel, check_exit, open_paper_trade

_NO_SLIP = SlippageModel(spread_fraction=0.0, min_slippage_per_share=0.0)

NOW = datetime(2026, 6, 1, tzinfo=UTC)
AS_OF = NOW.date()
EXP = date(2026, 7, 1)
DTE_Y = 30 / 365
VOL = 0.30


def _chain(spot: float = 100.0) -> OptionChain:
    cs = []
    for k in range(60, 141):
        for ot in (OptionType.CALL, OptionType.PUT):
            px = black_scholes_price(spot, k, DTE_Y, VOL, ot)
            if px < 0.02:
                continue
            cs.append(
                OptionContract(
                    symbol="AAA", expiration=EXP, strike=float(k), option_type=ot,
                    bid=round(px - 0.02, 2), ask=round(px + 0.02, 2), mark=round(px, 2),
                    volume=500, open_interest=2000,
                    greeks=Greeks(delta=round(black_scholes_delta(spot, k, DTE_Y, VOL, ot), 3)),
                    as_of=NOW,
                )
            )
    return OptionChain(symbol="AAA", underlying_price=spot, contracts=cs, as_of=NOW)


@pytest.fixture
def policy() -> RiskPolicy:
    return RiskPolicy(
        account_equity_usd=20_000.0, max_account_risk_pct=0.30, max_trade_risk_pct=0.10,
        max_concurrent_positions=8, max_defined_risk_per_trade_usd=1500.0,
        max_contracts_per_trade=20,
    )


def _bull_put(policy):
    choice = select_credit_vertical(_chain(), Direction.BULLISH, AS_OF, max_risk_usd=1500)
    return build_structure_plan(choice, policy, AS_OF)


def _condor(policy):
    choice = select_iron_condor(_chain(), AS_OF, max_risk_usd=1500)
    return build_structure_plan(choice, policy, AS_OF)


def test_credit_exit_is_pct_of_credit(policy: RiskPolicy) -> None:
    plan = _bull_put(policy)
    assert plan is not None
    # Entry is a net credit (negative signed net).
    trade = open_paper_trade(plan, "s", entry_mid=-1.00, slippage=_NO_SLIP)
    # Halving the credit owed (-1.00 -> -0.50) captures 50% -> profit target.
    assert check_exit(trade, -0.50) == ExitReason.PROFIT_TARGET
    # Credit widening to -1.50 loses 50% -> stop.
    assert check_exit(trade, -1.50) == ExitReason.STOP_LOSS
    assert check_exit(trade, -0.95) is None


def test_bull_put_wins_on_rally(policy: RiskPolicy) -> None:
    plan = _bull_put(policy)
    assert plan is not None
    path = [100 + i for i in range(11)] + [110] * 15
    res = backtest_trade(plan, 30, [float(x) for x in path], VOL, reprice_entry=True)
    assert res.realized_pnl_usd > 0
    assert res.exit_reason in (ExitReason.PROFIT_TARGET, ExitReason.TIME_STOP, ExitReason.EXPIRY)


def test_bull_put_loses_on_selloff(policy: RiskPolicy) -> None:
    plan = _bull_put(policy)
    assert plan is not None
    path = [100 - 2 * i for i in range(11)] + [78] * 15
    res = backtest_trade(plan, 30, [float(x) for x in path], VOL, reprice_entry=True)
    assert res.realized_pnl_usd < 0


def test_iron_condor_wins_when_range_bound(policy: RiskPolicy) -> None:
    plan = _condor(policy)
    assert plan is not None
    flat = [100, 100.5, 99.5, 100.2, 99.8, 100.1, 99.9, 100.0] + [100.0] * 20
    res = backtest_trade(plan, 30, flat, VOL, reprice_entry=True)
    assert res.realized_pnl_usd > 0


def test_iron_condor_loses_on_big_move(policy: RiskPolicy) -> None:
    plan = _condor(policy)
    assert plan is not None
    breakout = [100 + 1.5 * i for i in range(20)] + [130] * 10
    res = backtest_trade(plan, 30, [float(x) for x in breakout], VOL, reprice_entry=True)
    assert res.realized_pnl_usd < 0
