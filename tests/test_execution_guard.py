"""The execution guard must deny live execution by default and without approval."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.domain.enums import (
    Direction,
    OptionAction,
    OptionType,
    ProposalStatus,
    StrategyType,
)
from app.domain.trades import ContractLeg, OrderProposal, RiskPlan, TradePlan
from app.modes.execution_guard import ExecutionGuard
from app.risk.policy import RiskPolicy


def _proposal(status: ProposalStatus, max_loss: float = 36.0) -> OrderProposal:
    plan = TradePlan(
        symbol="AAA",
        direction=Direction.BULLISH,
        strategy=StrategyType.LONG_CALL,
        legs=[
            ContractLeg(
                symbol="AAA",
                action=OptionAction.BUY_TO_OPEN,
                option_type=OptionType.CALL,
                strike=100.0,
                expiration=date(2026, 8, 21),
                quantity=3,
                entry_price=0.12,
            )
        ],
        net_debit=12.0,
        contracts=3,
        risk=RiskPlan(
            max_loss_usd=max_loss,
            account_risk_pct=0.018,
            profit_target_pct=0.5,
            stop_loss_pct=0.5,
        ),
    )
    return OrderProposal(
        id="p1",
        scan_id="s1",
        symbol="AAA",
        status=status,
        trade_plan=plan,
        thesis_summary="test",
        created_at=datetime.now(UTC),
    )


def test_unapproved_proposal_denied() -> None:
    guard = ExecutionGuard(RiskPolicy.from_settings())
    decision = guard.authorize(_proposal(ProposalStatus.PENDING_APPROVAL))
    assert not decision.authorized
    assert decision.reason == "proposal_not_approved"


def test_approved_but_automation_off_denied() -> None:
    # Default settings: research mode, automation disabled.
    guard = ExecutionGuard(RiskPolicy.from_settings())
    decision = guard.authorize(_proposal(ProposalStatus.APPROVED))
    assert not decision.authorized
    # Denied because not in automation mode (double-gate).
    assert "no_live_execution" in decision.reason or "kill_switch" in decision.reason
