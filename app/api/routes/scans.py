"""Scan endpoints — run a research scan and browse ranked candidates + history."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.db.session import get_session
from app.domain.candidates import TradeCandidate
from app.engine.universe import UniverseConfig
from app.services.scan_service import run_scan

router = APIRouter(prefix="/scans", tags=["scans"])


class ScanRequest(BaseModel):
    symbols: list[str] | None = None


class ScanResponse(BaseModel):
    scan_id: str | None
    count: int
    actionable: int
    candidates: list[TradeCandidate]


class ScanSummary(BaseModel):
    scan_id: str
    candidate_count: int
    actionable_count: int
    created_at: str


@router.post("", response_model=ScanResponse)
async def create_scan(
    req: ScanRequest,
    actionable_only: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
) -> ScanResponse:
    universe = UniverseConfig(symbols=req.symbols) if req.symbols else UniverseConfig()
    candidates = await run_scan(universe=universe)
    scan_id = candidates[0].scan_id if candidates else None
    if candidates:
        await repository.save_scan(session, scan_id, universe.normalized_symbols(), candidates)

    shown = [c for c in candidates if c.is_actionable] if actionable_only else candidates
    return ScanResponse(
        scan_id=scan_id,
        count=len(candidates),
        actionable=sum(c.is_actionable for c in candidates),
        candidates=shown,
    )


@router.get("", response_model=list[ScanSummary])
async def list_scans(
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> list[ScanSummary]:
    rows = await repository.list_scans(session, limit=limit)
    return [
        ScanSummary(
            scan_id=r.scan_id,
            candidate_count=r.candidate_count,
            actionable_count=r.actionable_count,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]


@router.get("/{scan_id}/candidates", response_model=list[TradeCandidate])
async def scan_candidates(
    scan_id: str,
    actionable_only: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
) -> list[TradeCandidate]:
    cands = await repository.get_scan_candidates(session, scan_id)
    if not cands:
        raise HTTPException(404, "Scan not found or has no candidates.")
    return [c for c in cands if c.is_actionable] if actionable_only else cands
