"""Paper-trading endpoints (Mode 2) — open simulated trades and browse history.

Opens a simulated position from an actionable candidate (with slippage), then
tracks it. P&L / MFE / MAE marking is driven by the paper engine; this surface
persists the trades and exposes their history.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.db import repository
from app.domain.enums import OptionType
from app.domain.trades import PaperTrade
from app.quant.pricing import plan_entry_net_per_share
from app.services.paper_engine import open_paper_trade
from app.services.position_import import ImportedLeg, build_tracked_trade

router = APIRouter(prefix="/paper", tags=["paper"])


class OpenPaperRequest(BaseModel):
    scan_id: str
    symbol: str


class ImportLegIn(BaseModel):
    strike: float
    option_type: OptionType
    is_long: bool
    quantity: int
    entry_price_per_share: float
    expiration: date


class ImportPositionIn(BaseModel):
    symbol: str
    legs: list[ImportLegIn]
    opened_at: datetime | None = None


@router.post("/import", response_model=list[PaperTrade])
async def import_positions(positions: list[ImportPositionIn]) -> list[PaperTrade]:
    """Ingest real broker positions as tracked positions Tier 4 will monitor."""
    out: list[PaperTrade] = []
    for p in positions:
        legs = [
            ImportedLeg(
                strike=leg.strike, option_type=leg.option_type, is_long=leg.is_long,
                quantity=leg.quantity, entry_price_per_share=leg.entry_price_per_share,
                expiration=leg.expiration,
            )
            for leg in p.legs
        ]
        trade = build_tracked_trade(p.symbol, legs, opened_at=p.opened_at)
        await run_in_threadpool(repository.save_paper_trade, trade)
        out.append(trade)
    return out


async def _plan_entry_spread(plan) -> float:
    """Best-effort real bid/ask spread of the structure for realistic entry
    slippage. A chain miss degrades to 0.0 (the per-share floor still applies)."""
    from app.logging_config import get_logger
    from app.providers import registry
    from app.tiers.tier4_positions import structure_spread

    exps = sorted({lg.expiration for lg in plan.legs})
    try:
        chain = await registry.options_chain_provider().get_option_chain_for_expirations(
            plan.symbol, exps
        )
    except Exception as exc:  # noqa: BLE001 - never block an open on a chain miss
        get_logger(__name__).warning("paper_entry_spread_chain_failed", error=str(exc))
        return 0.0
    return structure_spread(plan, chain)


@router.post("", response_model=PaperTrade)
async def open_trade(req: OpenPaperRequest) -> PaperTrade:
    candidate = await run_in_threadpool(
        repository.get_candidate, req.scan_id, req.symbol
    )
    if candidate is None or not candidate.is_actionable or candidate.trade_plan is None:
        raise HTTPException(404, "No actionable candidate for that scan_id/symbol.")
    entry = plan_entry_net_per_share(candidate.trade_plan)
    entry_spread = await _plan_entry_spread(candidate.trade_plan)
    trade = open_paper_trade(
        candidate.trade_plan, req.scan_id, entry_mid=entry, entry_spread=entry_spread
    )
    await run_in_threadpool(repository.save_paper_trade, trade)
    return trade


@router.get("", response_model=list[PaperTrade])
async def list_trades(
    limit: int = Query(default=50, ge=1, le=200),
) -> list[PaperTrade]:
    return await run_in_threadpool(repository.list_paper_trades, limit)


@router.get("/{trade_id}", response_model=PaperTrade)
async def get_trade(trade_id: str) -> PaperTrade:
    t = await run_in_threadpool(repository.get_paper_trade, trade_id)
    if t is None:
        raise HTTPException(404, "Paper trade not found.")
    return t
