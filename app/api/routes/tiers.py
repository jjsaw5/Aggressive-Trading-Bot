"""Tier funnel endpoints — inspect tier membership and run a funnel pass.

Read/compute + persist only; the funnel never places orders. Running a pass is
allowed on demand regardless of the scheduler's TIERING_ENABLED flag (that flag
governs the automatic scheduled path, added at the Phase-5 cutover).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.db import repository
from app.engine.universe import UniverseConfig
from app.tiers.funnel import FunnelReport, build_funnel_engine
from app.tiers.models import TierMember

router = APIRouter(prefix="/tiers", tags=["tiers"])


class TierListing(BaseModel):
    broad: list[TierMember]
    watchlist: list[TierMember]
    candidates: list[TierMember]
    positions: list[TierMember]


class RunTierRequest(BaseModel):
    symbols: list[str] | None = None


@router.get("", response_model=TierListing)
async def list_tiers() -> TierListing:
    members = await run_in_threadpool(repository.list_all_tiers)
    by_tier: dict[int, list[TierMember]] = {1: [], 2: [], 3: [], 4: []}
    for m in members:
        by_tier[int(m.tier)].append(m)
    return TierListing(
        broad=by_tier[1],
        watchlist=by_tier[2],
        candidates=by_tier[3],
        positions=by_tier[4],
    )


@router.post("/run", response_model=FunnelReport)
async def run_funnel(req: RunTierRequest) -> FunnelReport:
    universe = UniverseConfig(symbols=req.symbols) if req.symbols else None
    engine = build_funnel_engine(universe=universe)
    return await engine.run_once(req.symbols)
