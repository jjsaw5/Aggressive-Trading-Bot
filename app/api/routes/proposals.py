"""Proposal endpoints (Mode 3) — create, view, approve, reject, and (gated) execute.

Approval marks a proposal APPROVED; it does NOT place an order. Live execution
is gated by the execution guard and disabled by default. Proposals persist to
the database.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.db import repository
from app.domain.trades import OrderProposal
from app.modes.execution_guard import ExecutionGuard
from app.modes.proposals import (
    ProposalError,
    approve_proposal,
    create_proposal,
    reject_proposal,
)

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
    candidate = await run_in_threadpool(
        repository.get_candidate, req.scan_id, req.symbol
    )
    if candidate is None:
        raise HTTPException(404, "Candidate not found for that scan_id/symbol.")
    try:
        proposal = create_proposal(candidate, ttl_minutes=req.ttl_minutes)
    except ProposalError as exc:
        raise HTTPException(409, str(exc)) from exc
    await run_in_threadpool(repository.save_proposal, proposal)
    return proposal


@router.get("", response_model=list[OrderProposal])
async def list_all(
    limit: int = Query(default=50, ge=1, le=200),
) -> list[OrderProposal]:
    return await run_in_threadpool(repository.list_proposals, limit)


@router.get("/{proposal_id}", response_model=OrderProposal)
async def get(proposal_id: str) -> OrderProposal:
    p = await run_in_threadpool(repository.get_proposal, proposal_id)
    if p is None:
        raise HTTPException(404, "Proposal not found.")
    return p


@router.post("/{proposal_id}/approve", response_model=OrderProposal)
async def approve(proposal_id: str, req: ApproveRequest) -> OrderProposal:
    p = await run_in_threadpool(repository.get_proposal, proposal_id)
    if p is None:
        raise HTTPException(404, "Proposal not found.")
    try:
        approve_proposal(p, req.approver)
    except ProposalError as exc:
        raise HTTPException(409, str(exc)) from exc
    await run_in_threadpool(repository.save_proposal, p)
    return p


@router.post("/{proposal_id}/reject", response_model=OrderProposal)
async def reject(proposal_id: str, req: RejectRequest) -> OrderProposal:
    p = await run_in_threadpool(repository.get_proposal, proposal_id)
    if p is None:
        raise HTTPException(404, "Proposal not found.")
    reject_proposal(p, req.note)
    await run_in_threadpool(repository.save_proposal, p)
    return p


@router.post("/{proposal_id}/execute")
async def execute(proposal_id: str) -> dict:
    """Attempt execution — passes through the guard. With automation disabled by
    default this ALWAYS returns authorized=false; the endpoint makes the safety
    gate observable and testable. No broker order is placed."""
    p = await run_in_threadpool(repository.get_proposal, proposal_id)
    if p is None:
        raise HTTPException(404, "Proposal not found.")
    decision = ExecutionGuard().authorize(p)
    return {
        "proposal_id": p.id,
        "authorized": decision.authorized,
        "reason": decision.reason,
        "note": "No broker order is placed by this platform build.",
    }
