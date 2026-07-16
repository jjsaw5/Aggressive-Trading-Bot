"""Scan endpoints — run a research scan and retrieve ranked candidates."""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.domain.candidates import TradeCandidate
from app.engine.universe import UniverseConfig
from app.services import store
from app.services.scan_service import run_scan

router = APIRouter(prefix="/scans", tags=["scans"])


class ScanRequest(BaseModel):
    symbols: list[str] | None = None


class ScanResponse(BaseModel):
    scan_id: str | None
    count: int
    actionable: int
    candidates: list[TradeCandidate]


@router.post("", response_model=ScanResponse)
async def create_scan(
    req: ScanRequest,
    actionable_only: bool = Query(default=False),
) -> ScanResponse:
    universe = UniverseConfig(symbols=req.symbols) if req.symbols else UniverseConfig()
    candidates = await run_scan(universe=universe)
    store.save_candidates(candidates)

    shown = [c for c in candidates if c.is_actionable] if actionable_only else candidates
    return ScanResponse(
        scan_id=candidates[0].scan_id if candidates else None,
        count=len(candidates),
        actionable=sum(c.is_actionable for c in candidates),
        candidates=shown,
    )
