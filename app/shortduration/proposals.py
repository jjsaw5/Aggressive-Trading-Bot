"""Short-duration human-approved live-proposal workflow (Phase 7, GATED OFF).

Builds on the existing OrderProposal + ExecutionGuard machinery. The candidate
lifecycle for the LIVE path is ARMED → TRIGGERED → PROPOSED → APPROVED → OPEN,
with approval an explicit, attributed human action. Execution is authorized ONLY
by the existing double-gate (`TRADING_MODE=automation` AND `AUTOMATION_ENABLED`);
with the defaults, `execute` always returns a DENIED decision and no order is
placed. This module places no order itself — it routes intent through the guard.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from app.db import repository
from app.domain.enums import CandidateState, ProposalStatus
from app.domain.trades import OrderProposal
from app.logging_config import get_logger
from app.modes.execution_guard import ExecutionDecision, ExecutionGuard
from app.modes.proposals import ProposalError, approve_proposal, reject_proposal
from app.shortduration.state import advance, transition

log = get_logger(__name__)

_SD_SCAN_PREFIX = "sd:"


def candidate_id_from(proposal: OrderProposal) -> str | None:
    if proposal.scan_id.startswith(_SD_SCAN_PREFIX):
        return proposal.scan_id[len(_SD_SCAN_PREFIX):]
    return None


def create_short_duration_proposal(candidate, *, ttl_minutes: int = 30, now: datetime | None = None) -> OrderProposal:
    if candidate.trade_plan is None:
        raise ProposalError(f"{candidate.symbol} has no sized contract to propose.")
    if candidate.state in {CandidateState.CLOSED, CandidateState.REJECTED, CandidateState.EXPIRED}:
        raise ProposalError(f"Candidate is {candidate.state.value}.")
    now = now or datetime.now(UTC)
    strat = candidate.strategy.value if candidate.strategy else "setup"
    return OrderProposal(
        id=uuid.uuid4().hex[:12],
        scan_id=f"{_SD_SCAN_PREFIX}{candidate.id}",
        symbol=candidate.symbol,
        status=ProposalStatus.PENDING_APPROVAL,
        trade_plan=candidate.trade_plan,
        thesis_summary=(
            f"{candidate.dte_category.value} {strat} {candidate.direction.value.upper()} "
            f"(score {candidate.score:.2f}) — {candidate.entry_trigger}"
        ),
        created_at=now,
        expires_at=now + timedelta(minutes=ttl_minutes),
    )


async def propose(candidate, *, now: datetime | None = None) -> OrderProposal:
    """Create a PENDING_APPROVAL ticket and advance the candidate to PROPOSED."""
    now = now or datetime.now(UTC)
    proposal = create_short_duration_proposal(candidate, now=now)
    await asyncio.to_thread(repository.save_proposal, proposal)
    for tr in advance(candidate, CandidateState.PROPOSED, trigger="propose", actor="dashboard",
                      reason="Live proposal created (pending approval).", at=now):
        await asyncio.to_thread(repository.append_candidate_transition, tr)
    await asyncio.to_thread(repository.save_short_duration_candidate, candidate)
    log.info("sd_proposed", symbol=candidate.symbol, proposal_id=proposal.id)
    return proposal


async def approve(proposal_id: str, approver: str, *, now: datetime | None = None) -> OrderProposal:
    now = now or datetime.now(UTC)
    proposal = await asyncio.to_thread(repository.get_proposal, proposal_id)
    if proposal is None:
        raise ProposalError("Proposal not found.")
    approve_proposal(proposal, approver, now=now)
    await asyncio.to_thread(repository.save_proposal, proposal)
    cid = candidate_id_from(proposal)
    if cid:
        cand = await asyncio.to_thread(repository.get_short_duration_candidate, cid)
        if cand and cand.state == CandidateState.PROPOSED:
            tr = transition(cand, CandidateState.APPROVED, trigger="approve", actor=approver,
                            reason=f"Approved by {approver}.", at=now)
            await asyncio.to_thread(repository.append_candidate_transition, tr)
            await asyncio.to_thread(repository.save_short_duration_candidate, cand)
    return proposal


async def reject(proposal_id: str, note: str | None = None, *, now: datetime | None = None) -> OrderProposal:
    now = now or datetime.now(UTC)
    proposal = await asyncio.to_thread(repository.get_proposal, proposal_id)
    if proposal is None:
        raise ProposalError("Proposal not found.")
    reject_proposal(proposal, note)
    await asyncio.to_thread(repository.save_proposal, proposal)
    cid = candidate_id_from(proposal)
    if cid:
        cand = await asyncio.to_thread(repository.get_short_duration_candidate, cid)
        if cand and cand.state not in {CandidateState.CLOSED, CandidateState.REJECTED, CandidateState.EXPIRED}:
            tr = transition(cand, CandidateState.REJECTED, trigger="reject", actor="dashboard",
                            reason=note or "Proposal rejected.", at=now)
            await asyncio.to_thread(repository.append_candidate_transition, tr)
            await asyncio.to_thread(repository.save_short_duration_candidate, cand)
    return proposal


async def execute(proposal_id: str, *, now: datetime | None = None) -> ExecutionDecision:
    """Route the approved proposal through the ExecutionGuard. Returns the guard's
    decision — DENIED under the safe defaults (research mode, automation off). No
    order is placed here; a live placement path is only reachable when the double
    gate is explicitly armed, which this module does not do."""
    now = now or datetime.now(UTC)
    proposal = await asyncio.to_thread(repository.get_proposal, proposal_id)
    if proposal is None:
        raise ProposalError("Proposal not found.")
    decision = ExecutionGuard().authorize(proposal)
    log.info("sd_execute_decision", proposal_id=proposal_id,
             authorized=decision.authorized, reason=decision.reason)
    # Even when authorized, no broker order is placed (no order path is wired);
    # the guard decision is surfaced so the safety gate stays observable/testable.
    return decision


def list_sd_proposals(limit: int = 100) -> list[OrderProposal]:
    return [p for p in repository.list_proposals(limit=limit)
            if p.scan_id.startswith(_SD_SCAN_PREFIX)]
