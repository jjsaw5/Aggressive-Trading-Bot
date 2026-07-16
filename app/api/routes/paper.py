"""Paper-trading endpoints (Mode 2) — open simulated trades and browse history.

Opens a simulated position from an actionable candidate (with slippage), then
tracks it. P&L / MFE / MAE marking is driven by the paper engine; this surface
persists the trades and exposes their history.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.db.session import get_session
from app.domain.trades import PaperTrade
from app.quant.pricing import plan_entry_net_per_share
from app.services.paper_engine import open_paper_trade

router = APIRouter(prefix="/paper", tags=["paper"])


class OpenPaperRequest(BaseModel):
    scan_id: str
    symbol: str


@router.post("", response_model=PaperTrade)
async def open_trade(
    req: OpenPaperRequest, session: AsyncSession = Depends(get_session)
) -> PaperTrade:
    candidate = await repository.get_candidate(session, req.scan_id, req.symbol)
    if candidate is None or not candidate.is_actionable or candidate.trade_plan is None:
        raise HTTPException(404, "No actionable candidate for that scan_id/symbol.")
    entry = plan_entry_net_per_share(candidate.trade_plan)
    trade = open_paper_trade(candidate.trade_plan, req.scan_id, entry_mid=entry)
    await repository.save_paper_trade(session, trade)
    return trade


@router.get("", response_model=list[PaperTrade])
async def list_trades(
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[PaperTrade]:
    return await repository.list_paper_trades(session, limit=limit)


@router.get("/{trade_id}", response_model=PaperTrade)
async def get_trade(
    trade_id: str, session: AsyncSession = Depends(get_session)
) -> PaperTrade:
    t = await repository.get_paper_trade(session, trade_id)
    if t is None:
        raise HTTPException(404, "Paper trade not found.")
    return t
