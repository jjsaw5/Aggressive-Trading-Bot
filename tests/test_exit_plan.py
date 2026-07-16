"""Exit-plan level math for debit spreads, credit spreads, and long options."""

from __future__ import annotations

from datetime import date

from app.domain.enums import Direction, OptionAction, OptionType, StrategyType
from app.domain.trades import ContractLeg, RiskPlan, SpreadAnalytics, TradePlan
from app.risk.exit_plan import (
    credit_vertical_exit,
    debit_vertical_exit,
    for_trade_plan,
    long_option_exit,
)


def _levels(plan):
    return {lvl.label: lvl for lvl in plan.levels}


def test_debit_vertical_levels() -> None:
    plan = debit_vertical_exit(debit=1.5, width=5.0, contracts=2)
    assert plan.max_profit_usd == 700.0  # (5-1.5)*100*2
    assert plan.max_loss_usd == 300.0
    lv = _levels(plan)
    assert lv["Take profit (50% of max)"].net_price == 3.25
    assert lv["Take profit (50% of max)"].pnl_usd == 350.0
    assert lv["Stop (-50% of debit)"].net_price == 0.75
    assert lv["Stop (-50% of debit)"].pnl_usd == -150.0
    assert plan.action == "sell_to_close"
    assert any(x.kind == "time_stop" for x in plan.levels)


def test_credit_vertical_levels() -> None:
    plan = credit_vertical_exit(credit=1.0, width=5.0, contracts=1)
    assert plan.max_profit_usd == 100.0  # keep the credit
    assert plan.max_loss_usd == 400.0
    lv = _levels(plan)
    # Buy back at half the credit -> capture 50%.
    assert lv["Take profit (50% of credit)"].net_price == 0.50
    assert lv["Take profit (50% of credit)"].pnl_usd == 50.0
    assert plan.action == "buy_to_close"
    stop = lv["Stop (1x credit)"]
    assert stop.net_price == 2.0  # doubled
    assert stop.pnl_usd == -100.0


def test_long_option_levels_uncapped() -> None:
    plan = long_option_exit(premium=2.0, contracts=1)
    assert plan.max_profit_usd is None  # uncapped
    assert plan.max_loss_usd == 200.0
    lv = _levels(plan)
    assert lv["Take profit (+50%)"].net_price == 3.0
    assert lv["Take profit (+50%)"].pnl_usd == 100.0
    assert lv["Take profit (+100%)"].net_price == 4.0
    assert lv["Stop (-50%)"].net_price == 1.0


def _bull_call_plan() -> TradePlan:
    plan = TradePlan(
        symbol="AAA",
        direction=Direction.BULLISH,
        strategy=StrategyType.BULL_CALL_SPREAD,
        legs=[
            ContractLeg(symbol="AAA", action=OptionAction.BUY_TO_OPEN, option_type=OptionType.CALL,
                        strike=100.0, expiration=date(2026, 9, 18), quantity=1, entry_price=3.0),
            ContractLeg(symbol="AAA", action=OptionAction.SELL_TO_OPEN, option_type=OptionType.CALL,
                        strike=105.0, expiration=date(2026, 9, 18), quantity=1, entry_price=1.5),
        ],
        net_debit=150.0,  # $1.50/share
        contracts=1,
        risk=RiskPlan(max_loss_usd=150.0, max_profit_usd=350.0, account_risk_pct=0.075,
                      profit_target_pct=0.5, stop_loss_pct=0.5, time_stop_dte=7),
        analytics=SpreadAnalytics(breakevens=[101.5]),
    )
    return plan


def test_for_trade_plan_debit_vertical() -> None:
    plan = _bull_call_plan()
    ep = for_trade_plan(plan)
    assert ep.method == "debit_vertical"
    assert ep.max_loss_usd == 150.0
    assert ep.max_profit_usd == 350.0  # width 5 - debit 1.5 = 3.5 -> $350
    assert ep.breakevens == [101.5]
    lv = _levels(ep)
    # 50% of max profit -> net 1.5 + 0.5*3.5 = 3.25
    assert lv["Take profit (50% of max)"].net_price == 3.25


def test_for_trade_plan_credit_uses_buy_to_close() -> None:
    plan = _bull_call_plan()
    plan.strategy = StrategyType.BULL_PUT_SPREAD
    plan.net_debit = -100.0  # $1.00 credit
    ep = for_trade_plan(plan)
    assert ep.method == "credit_vertical"
    assert ep.action == "buy_to_close"
    assert ep.max_profit_usd == 100.0
