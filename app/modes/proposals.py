"""Proposal service (Mode 3: human approval).

Converts an actionable `TradeCandidate` into an `OrderProposal` ticket in
PENDING_APPROVAL state. Approval/rejection are explicit, attributed actions —
there is no auto-approve path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.domain.candidates import TradeCandidate
from app.domain.enums import ProposalStatus
from app.domain.trades import OrderProposal


class ProposalError(RuntimeError):
    pass


def create_proposal(
    candidate: TradeCandidate, *, ttl_minutes: int = 30, now: datetime | None = None
) -> OrderProposal:
    if not candidate.is_actionable or candidate.trade_plan is None:
        raise ProposalError(
            f"Candidate {candidate.symbol} is not actionable (status={candidate.status})."
        )
    now = now or datetime.now(UTC)
    return OrderProposal(
        id=uuid.uuid4().hex[:12],
        scan_id=candidate.scan_id,
        symbol=candidate.symbol,
        status=ProposalStatus.PENDING_APPROVAL,
        trade_plan=candidate.trade_plan,
        thesis_summary=(
            f"{candidate.direction.value.upper()} — {candidate.thesis.why_now} "
            f"(score {candidate.composite_score:.2f})"
        ),
        created_at=now,
        expires_at=now + timedelta(minutes=ttl_minutes),
    )


def approve_proposal(
    proposal: OrderProposal, approver: str, now: datetime | None = None
) -> OrderProposal:
    now = now or datetime.now(UTC)
    if proposal.status != ProposalStatus.PENDING_APPROVAL:
        raise ProposalError(f"Cannot approve proposal in state {proposal.status}.")
    if proposal.expires_at and now > proposal.expires_at:
        proposal.status = ProposalStatus.EXPIRED
        raise ProposalError("Proposal has expired; re-scan for a fresh ticket.")
    proposal.status = ProposalStatus.APPROVED
    proposal.approved_by = approver
    proposal.approved_at = now
    return proposal


def reject_proposal(
    proposal: OrderProposal, note: str | None = None
) -> OrderProposal:
    proposal.status = ProposalStatus.REJECTED
    proposal.reject_note = note
    return proposal
