"""Multi-leg structure selection, sizing, and analytics."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.domain.enums import Direction, OptionType, StrategyType
from app.domain.options import Greeks, OptionChain, OptionContract
from app.engine.contract_selection import (
    select_credit_vertical,
    select_iron_condor,
    select_straddle,
    select_strangle,
)
from app.engine.strategy_selector import build_best_plan
from app.quant.analytics import compute_analytics
from app.quant.pricing import black_scholes_delta, black_scholes_price
from app.risk.policy import RiskPolicy
from app.risk.trade_plan import build_structure_plan

NOW = datetime(2026, 6, 1, tzinfo=UTC)
AS_OF = NOW.date()
EXP = date(2026, 7, 1)  # ~30 DTE
DTE_Y = 30 / 365
VOL = 0.30


def _chain(spot: float = 100.0, lo: int = 60, hi: int = 140) -> OptionChain:
    """BS-priced chain with $1 strikes so all structures are constructible."""
    contracts: list[OptionContract] = []
    for k in range(lo, hi + 1):
        for ot in (OptionType.CALL, OptionType.PUT):
            px = black_scholes_price(spot, k, DTE_Y, VOL, ot)
            if px < 0.02:
                continue
            dlt = black_scholes_delta(spot, k, DTE_Y, VOL, ot)
            contracts.append(
                OptionContract(
                    symbol="AAA", expiration=EXP, strike=float(k), option_type=ot,
                    bid=round(px - 0.02, 2), ask=round(px + 0.02, 2), mark=round(px, 2),
                    volume=500, open_interest=2000,
                    greeks=Greeks(delta=round(dlt, 3)), as_of=NOW,
                )
            )
    return OptionChain(symbol="AAA", underlying_price=spot, contracts=contracts, as_of=NOW)


@pytest.fixture
def roomy_policy() -> RiskPolicy:
    return RiskPolicy(
        account_equity_usd=5_000.0, max_account_risk_pct=0.30, max_trade_risk_pct=0.10,
        max_concurrent_positions=8, max_defined_risk_per_trade_usd=500.0,
        max_contracts_per_trade=20,
    )


# --- Selection ---------------------------------------------------------------
def test_credit_vertical_bull_put() -> None:
    choice = select_credit_vertical(_chain(), Direction.BULLISH, AS_OF, max_risk_usd=500)
    assert choice is not None
    assert choice.strategy == StrategyType.BULL_PUT_SPREAD
    assert choice.net_debit_per_share < 0  # credit
    # Short put strike above the long (protective) put strike.
    short, long = choice.legs
    assert short.contract.strike > long.contract.strike
    assert choice.max_loss_per_contract <= 500


def test_straddle_is_atm_call_and_put() -> None:
    choice = select_straddle(_chain(spot=100.0), AS_OF, max_debit_usd=5000)
    assert choice is not None
    assert choice.strategy == StrategyType.LONG_STRADDLE
    strikes = {leg.contract.strike for leg in choice.legs}
    assert strikes == {100.0}
    assert choice.max_profit_per_contract is None  # uncapped


def test_strangle_legs_out_of_the_money() -> None:
    choice = select_strangle(_chain(spot=100.0), AS_OF, max_debit_usd=5000)
    assert choice is not None
    types = {leg.contract.option_type for leg in choice.legs}
    assert types == {OptionType.CALL, OptionType.PUT}


def test_iron_condor_four_legs_ordered() -> None:
    choice = select_iron_condor(_chain(), AS_OF, max_risk_usd=500)
    assert choice is not None
    assert choice.strategy == StrategyType.IRON_CONDOR
    assert len(choice.legs) == 4
    assert choice.net_debit_per_share < 0  # net credit


# --- Sizing ------------------------------------------------------------------
def test_build_structure_plan_respects_cap(roomy_policy: RiskPolicy) -> None:
    choice = select_credit_vertical(_chain(), Direction.BULLISH, AS_OF, max_risk_usd=500)
    assert choice is not None
    plan = build_structure_plan(choice, roomy_policy, AS_OF)
    assert plan is not None
    assert plan.risk.max_loss_usd <= roomy_policy.max_trade_risk_usd + 1e-6
    assert len(plan.legs) == 2


# --- Analytics ---------------------------------------------------------------
def test_analytics_bull_call_pop_and_breakeven(roomy_policy: RiskPolicy) -> None:
    from app.engine.contract_selection import select_vertical_spread
    from app.risk.trade_plan import build_vertical_spread_plan

    spread = select_vertical_spread(_chain(), Direction.BULLISH, AS_OF, max_debit_usd=500)
    assert spread is not None
    plan = build_vertical_spread_plan(spread, Direction.BULLISH, roomy_policy, AS_OF)
    assert plan is not None
    a = compute_analytics(plan, 100.0, VOL, AS_OF)
    assert len(a.breakevens) == 1
    assert 0.0 <= a.probability_of_profit <= 1.0
    assert a.net_delta > 0  # bullish debit spread is long delta
    assert not a.is_credit


def test_analytics_iron_condor_region(roomy_policy: RiskPolicy) -> None:
    choice = select_iron_condor(_chain(), AS_OF, max_risk_usd=500)
    assert choice is not None
    plan = build_structure_plan(choice, roomy_policy, AS_OF)
    assert plan is not None
    a = compute_analytics(plan, 100.0, VOL, AS_OF)
    assert len(a.breakevens) == 2
    lo, hi = a.breakevens
    assert lo < 100.0 < hi  # profit region brackets spot
    assert a.is_credit
    # ATM condor should have a healthy probability of profit.
    assert a.probability_of_profit is not None and a.probability_of_profit > 0.4


def test_analytics_straddle_two_breakevens() -> None:
    # ATM straddles are expensive (~$700/contract); give it a fitting budget.
    big = RiskPolicy(
        account_equity_usd=20_000.0, max_account_risk_pct=0.30, max_trade_risk_pct=0.10,
        max_concurrent_positions=8, max_defined_risk_per_trade_usd=1500.0,
        max_contracts_per_trade=20,
    )
    choice = select_straddle(_chain(spot=100.0), AS_OF, max_debit_usd=5000)
    assert choice is not None
    plan = build_structure_plan(choice, big, AS_OF)
    assert plan is not None
    a = compute_analytics(plan, 100.0, VOL, AS_OF)
    assert len(a.breakevens) == 2
    lo, hi = a.breakevens
    assert lo < 100.0 < hi
    # Roughly delta-neutral per contract (small vs a single option's ~100 delta).
    assert abs(a.net_delta / plan.contracts) < 15


# --- Strategy routing --------------------------------------------------------
def test_high_iv_bullish_prefers_credit(roomy_policy: RiskPolicy) -> None:
    plan = build_best_plan(_chain(), Direction.BULLISH, iv_rank=0.85,
                           has_catalyst=False, policy=roomy_policy, as_of=AS_OF)
    assert plan is not None
    assert plan.strategy == StrategyType.BULL_PUT_SPREAD  # credit chosen in high IV


def test_low_iv_bullish_prefers_debit(roomy_policy: RiskPolicy) -> None:
    plan = build_best_plan(_chain(), Direction.BULLISH, iv_rank=0.15,
                           has_catalyst=False, policy=roomy_policy, as_of=AS_OF)
    assert plan is not None
    assert plan.strategy in (StrategyType.BULL_CALL_SPREAD, StrategyType.LONG_CALL)


def test_neutral_high_iv_builds_condor(roomy_policy: RiskPolicy) -> None:
    plan = build_best_plan(_chain(), Direction.NEUTRAL, iv_rank=0.80,
                           has_catalyst=False, policy=roomy_policy, as_of=AS_OF)
    assert plan is not None
    assert plan.strategy == StrategyType.IRON_CONDOR


def test_neutral_low_iv_with_catalyst_builds_long_vol(roomy_policy: RiskPolicy) -> None:
    plan = build_best_plan(_chain(), Direction.NEUTRAL, iv_rank=0.20,
                           has_catalyst=True, policy=roomy_policy, as_of=AS_OF)
    assert plan is not None
    assert plan.strategy in (StrategyType.LONG_STRANGLE, StrategyType.LONG_STRADDLE)


def test_neutral_mid_iv_no_trade(roomy_policy: RiskPolicy) -> None:
    plan = build_best_plan(_chain(), Direction.NEUTRAL, iv_rank=0.45,
                           has_catalyst=False, policy=roomy_policy, as_of=AS_OF)
    assert plan is None
