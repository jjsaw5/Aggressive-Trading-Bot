"""Execution guard — the single chokepoint for live order placement.

NOTHING in this codebase should call a brokerage `place_order` directly. All
execution intents pass through `ExecutionGuard.authorize`, which enforces, in
order:

  1. The global automation kill-switch AND automation mode are both on
     (`settings.automation_armed`). Default: OFF.
  2. The order references a human-APPROVED proposal (approval can never be
     implied).
  3. The proposal's defined risk still fits the active risk policy.

Any failure returns a denied `ExecutionDecision` with a reason and is logged.
Automation is disabled by default and unrestricted autonomous trading is not
representable by this API — approval is always required.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import TradingMode, settings
from app.domain.enums import ProposalStatus
from app.domain.trades import OrderProposal
from app.logging_config import get_logger
from app.risk.policy import RiskPolicy

log = get_logger(__name__)


@dataclass(frozen=True)
class ExecutionDecision:
    authorized: bool
    reason: str


def _conviction_block() -> str | None:
    """Deny reason while the conviction gate is RED, else None.

    Fails CLOSED: if the gate cannot be evaluated at all, that is not permission.
    An engine that cannot demonstrate calibration is indistinguishable, from the
    broker's side, from one that has none."""
    try:
        from app.shortduration.conviction_gate import get_conviction_gate

        gate = get_conviction_gate()
    except Exception as exc:  # noqa: BLE001 — unevaluable gate denies, never allows
        log.warning("conviction_gate_unevaluable_denying", error=str(exc))
        return "conviction_gate_unevaluable"
    if not gate.green:
        failed = ",".join(c.name for c in gate.criteria if not c.passed)
        return f"conviction_gate_red[{failed}]"
    return None


class ExecutionGuard:
    def __init__(self, policy: RiskPolicy | None = None) -> None:
        self.policy = policy or RiskPolicy.from_settings()

    def authorize(self, proposal: OrderProposal) -> ExecutionDecision:
        # 1. Approval is mandatory in every mode. No exceptions.
        if proposal.status != ProposalStatus.APPROVED:
            return self._deny(proposal, "proposal_not_approved")

        # 2. Live automated placement requires the double-gate.
        if settings.trading_mode != TradingMode.AUTOMATION:
            return self._deny(proposal, f"mode_{settings.trading_mode.value}_no_live_execution")
        if not settings.automation_armed:
            return self._deny(proposal, "automation_kill_switch_off")

        # 3. Risk must still fit policy at execution time.
        if proposal.trade_plan.risk.max_loss_usd > self.policy.max_trade_risk_usd + 1e-6:
            return self._deny(proposal, "risk_exceeds_policy_at_execution")

        # 4. The engine must have earned the right to spend money. Signal-only
        #    capture mode: while the conviction gate is RED, no scanner-derived
        #    intent may reach a broker, whatever else is configured.
        #
        #    This is deliberately the LAST check and deliberately not
        #    configurable. The audit of build 7afa098 found 67 scanner signals
        #    spanning 4 sessions, 78% one direction, score-vs-outcome
        #    indistinguishable from zero. The gate was already red and correct to
        #    be; it simply was not wired to execution. A gate nothing consults is
        #    documentation.
        gate_reason = _conviction_block()
        if gate_reason is not None:
            return self._deny(proposal, gate_reason)

        log.info(
            "execution_authorized",
            proposal_id=proposal.id,
            symbol=proposal.symbol,
            max_loss_usd=proposal.trade_plan.risk.max_loss_usd,
        )
        return ExecutionDecision(authorized=True, reason="authorized")

    def _deny(self, proposal: OrderProposal, reason: str) -> ExecutionDecision:
        log.warning(
            "execution_denied",
            proposal_id=proposal.id,
            symbol=proposal.symbol,
            reason=reason,
        )
        return ExecutionDecision(authorized=False, reason=reason)
