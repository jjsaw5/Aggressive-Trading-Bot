"""Outcome-tracking endpoints — the platform's self-scoring surface.

Browse warehoused decisions, trigger a resolution pass against current prices,
and read the calibration scorecard (win rate, direction accuracy, POP
calibration, Brier score). This is how the system grades its own suggestions.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.analytics.calibration import Scorecard, build_scorecard
from app.db import repository
from app.domain.outcomes import DecisionOutcome, DecisionSnapshot
from app.services.outcomes_service import resolve_pending

router = APIRouter(prefix="/outcomes", tags=["outcomes"])


class ResolveResponse(BaseModel):
    resolved: int
    outcomes: list[DecisionOutcome]


class DecisionDetail(BaseModel):
    snapshot: DecisionSnapshot
    outcomes: list[DecisionOutcome]


@router.get("/snapshots", response_model=list[DecisionSnapshot])
async def snapshots(
    limit: int = Query(default=100, ge=1, le=1000),
    status: str | None = Query(default=None, pattern="^(pending|resolved)$"),
) -> list[DecisionSnapshot]:
    return await run_in_threadpool(repository.list_snapshots, limit, status)


@router.post("/resolve", response_model=ResolveResponse)
async def resolve(
    min_age_days: int = Query(default=1, ge=0, le=365),
    at_expiry_only: bool = Query(default=False),
    limit: int = Query(default=500, ge=1, le=1000),
) -> ResolveResponse:
    outcomes = await resolve_pending(
        min_age_days=min_age_days, at_expiry_only=at_expiry_only, limit=limit
    )
    return ResolveResponse(resolved=len(outcomes), outcomes=outcomes)


@router.get("/calibration", response_model=Scorecard)
async def calibration(
    limit: int = Query(default=1000, ge=1, le=5000),
) -> Scorecard:
    snaps, outs = await run_in_threadpool(repository.fetch_calibration_data, limit)
    return build_scorecard(snaps, outs)


@router.get("/{decision_id:path}", response_model=DecisionDetail)
async def decision_detail(decision_id: str) -> DecisionDetail:
    snap = await run_in_threadpool(repository.get_snapshot, decision_id)
    if snap is None:
        raise HTTPException(404, "Decision not found.")
    outs = await run_in_threadpool(repository.get_outcomes_for, decision_id)
    return DecisionDetail(snapshot=snap, outcomes=outs)
