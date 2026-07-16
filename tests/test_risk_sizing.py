"""Position sizing must never breach the per-trade or account risk caps."""

from __future__ import annotations

from app.risk.policy import RiskPolicy
from app.risk.portfolio import OpenPosition, PortfolioState, evaluate_admission
from app.risk.position_sizing import size_by_defined_risk


def test_per_trade_cap_is_min_of_pct_and_abs(policy: RiskPolicy) -> None:
    # 2% of $2000 = $40, absolute cap $100 -> tighter is $40.
    assert policy.max_trade_risk_usd == 40.0


def test_sizing_respects_trade_cap(policy: RiskPolicy) -> None:
    # $12/contract risk, $40 cap -> 3 contracts ($36), never 4 ($48).
    result = size_by_defined_risk(12.0, policy)
    assert result.contracts == 3
    assert result.max_loss_usd == 36.0
    assert result.max_loss_usd <= policy.max_trade_risk_usd


def test_single_contract_too_expensive_is_not_tradeable(policy: RiskPolicy) -> None:
    result = size_by_defined_risk(75.0, policy)  # one lot exceeds $40 cap
    assert result.contracts == 0
    assert not result.is_tradeable
    assert result.capped_reason == "single_contract_exceeds_trade_cap"


def test_account_cap_limits_new_trade(policy: RiskPolicy) -> None:
    # Account cap = 6% of $2000 = $120. Already $110 at risk -> only $10 left.
    result = size_by_defined_risk(12.0, policy, open_risk_usd=110.0)
    assert result.contracts == 0
    assert result.capped_reason == "account_cap_exhausted"


def test_contract_count_cap_applied(policy: RiskPolicy) -> None:
    # Very cheap per-contract risk ($0.50) would size to 80 under a $40 cap;
    # the contract-count cap must clamp it to max_contracts_per_trade.
    result = size_by_defined_risk(0.50, policy)
    assert result.contracts == policy.max_contracts_per_trade
    assert result.capped_reason == "contract_count_cap"
    assert result.max_loss_usd <= policy.max_trade_risk_usd


def test_zero_or_negative_risk_rejected(policy: RiskPolicy) -> None:
    assert size_by_defined_risk(0.0, policy).contracts == 0
    assert size_by_defined_risk(-5.0, policy).contracts == 0


def test_portfolio_admission_blocks_when_full(policy: RiskPolicy) -> None:
    positions = [OpenPosition(f"S{i}", 20.0) for i in range(policy.max_concurrent_positions)]
    portfolio = PortfolioState(positions=positions)
    decision = evaluate_admission(10.0, portfolio, policy)
    assert not decision.admitted


def test_portfolio_admission_blocks_when_account_heat_exceeded(policy: RiskPolicy) -> None:
    portfolio = PortfolioState(positions=[OpenPosition("A", 115.0)])
    decision = evaluate_admission(10.0, portfolio, policy)  # 115 + 10 = 125 > 120
    assert not decision.admitted
