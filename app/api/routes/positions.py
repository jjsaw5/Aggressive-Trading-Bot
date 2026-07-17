"""Live positions view — every open tracked position marked by Tier 4.

Runs the Tier-4 monitor over open positions and returns per-position P&L and the
mechanical exit action (hold / take_profit / stop / time_stop / unmarked). This
is the surface you watch to manage risk; it never places orders.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.db import repository
from app.domain.enums import PaperTradeStatus
from app.providers import registry
from app.tiers.tier4_positions import Tier4PositionMonitor

router = APIRouter(prefix="/positions", tags=["positions"])


class PositionView(BaseModel):
    symbol: str
    strategy: str
    contracts: int
    entry_net: float
    current_net: float | None = None
    pnl_usd: float | None = None
    pnl_pct: float | None = None
    dte: int | None = None
    action: str = "unmarked"
    note: str = ""


@router.get("", response_model=list[PositionView])
async def list_positions() -> list[PositionView]:
    trades = await run_in_threadpool(repository.list_paper_trades, 200)
    open_trades = [t for t in trades if t.status == PaperTradeStatus.OPEN]
    # De-dupe to one row per symbol (latest opened), so re-imports don't double up.
    latest: dict[str, object] = {}
    for t in sorted(open_trades, key=lambda x: x.opened_at):
        latest[t.symbol] = t
    open_trades = list(latest.values())

    monitor = Tier4PositionMonitor(chain=registry.options_chain_provider())
    risks = await monitor.run(open_trades)
    by_id = {r.trade_id: r for r in risks}

    rows: list[PositionView] = []
    for t in open_trades:
        r = by_id.get(t.id)
        rows.append(
            PositionView(
                symbol=t.symbol,
                strategy=t.trade_plan.strategy.value,
                contracts=t.trade_plan.contracts,
                entry_net=t.entry_fill,
                current_net=r.current_net if r else None,
                pnl_usd=r.pnl_usd if r else None,
                pnl_pct=r.pnl_pct if r else None,
                dte=r.dte if r else None,
                action=r.action if r else "unmarked",
                note=r.note if r else "",
            )
        )
    # Surface risk first: stops/take-profits/expiries above holds.
    order = {"stop": 0, "take_profit": 1, "time_stop": 2, "unmarked": 3, "hold": 4}
    rows.sort(key=lambda x: (order.get(x.action, 5), -(x.pnl_usd or 0)))
    return rows
