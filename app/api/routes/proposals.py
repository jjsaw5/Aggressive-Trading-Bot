"""Proposal endpoints (Mode 3) — create, view, approve, reject.

Approval here marks a proposal APPROVED; it does NOT place an order. Live
execution is gated separately by the execution guard and disabled by default.
The `dry_run` execution endpoint demonstrates the guard without any broker call.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.domain.trades import OrderProposal
from app.modes.execution_guard import ExecutionGuard
from app.modes.proposals import (
    ProposalError,
    approve_proposal,
    create_proposal,
    reject_proposal,
)
from app.services import store

router = APIRouter(prefix="/proposals", tags=["proposals"])


class CreateProposalRequest(BaseModel):
    scan_id: str
    symbol: str
    ttl_minutes: int = 30


class ApproveRequest(BaseModel):
    approver: str


class RejectRequest(BaseModel):
    note: str | None = None


@router.post("", response_model=OrderProposal)
async def create(req: CreateProposalRequest) -> OrderProposal:
    candidate = store.get_candidate(req.scan_id, req.symbol)
    if candidate is None:
        raise HTTPException(404, "Candidate not found for that scan_id/symbol.")
    try:
        proposal = create_proposal(candidate, ttl_minutes=req.ttl_minutes)
    except ProposalError as exc:
        raise HTTPException(409, str(exc)) from exc
    store.save_proposal(proposal)
    return proposal


@router.get("", response_model=list[OrderProposal])
async def list_all() -> list[OrderProposal]:
    return store.list_proposals()


@router.get("/{proposal_id}", response_model=OrderProposal)
async def get(proposal_id: str) -> OrderProposal:
    p = store.get_proposal(proposal_id)
    if p is None:
        raise HTTPException(404, "Proposal not found.")
    return p


@router.post("/{proposal_id}/approve", response_model=OrderProposal)
async def approve(proposal_id: str, req: ApproveRequest) -> OrderProposal:
    p = store.get_proposal(proposal_id)
    if p is None:
        raise HTTPException(404, "Proposal not found.")
    try:
        approve_proposal(p, req.approver)
    except ProposalError as exc:
        raise HTTPException(409, str(exc)) from exc
    store.save_proposal(p)
    return p


@router.post("/{proposal_id}/reject", response_model=OrderProposal)
async def reject(proposal_id: str, req: RejectRequest) -> OrderProposal:
    p = store.get_proposal(proposal_id)
    if p is None:
        raise HTTPException(404, "Proposal not found.")
    reject_proposal(p, req.note)
    store.save_proposal(p)
    return p


@router.post("/{proposal_id}/execute")
async def execute(proposal_id: str) -> dict:
    """Attempt execution — passes through the guard. Returns the guard decision.

    With automation disabled by default this ALWAYS returns authorized=false;
    the endpoint exists to make the safety gate observable and testable.
    """
    p = store.get_proposal(proposal_id)
    if p is None:
        raise HTTPException(404, "Proposal not found.")
    decision = ExecutionGuard().authorize(p)
    return {
        "proposal_id": p.id,
        "authorized": decision.authorized,
        "reason": decision.reason,
        "note": "No broker order is placed by this platform build.",
    }
