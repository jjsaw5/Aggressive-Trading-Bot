"""Live positions view — every open tracked position marked by Tier 4.

Runs the Tier-4 monitor over open positions and returns, per position: P&L, the
mechanical exit action, the full exit plan (a constant reminder of where to
close), the close-order ticket (legs reversed), risk economics, and warnings.
Also supports syncing positions from the configured brokerage. Never places
orders.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.db import repository
from app.domain.enums import OptionAction, PaperTradeStatus
from app.domain.trades import PaperTrade
from app.providers import registry
from app.tiers.models import PositionRisk
from app.tiers.tier4_positions import Tier4PositionMonitor

router = APIRouter(prefix="/positions", tags=["positions"])

_BUY = {OptionAction.BUY_TO_OPEN, OptionAction.BUY_TO_CLOSE}


class LegView(BaseModel):
    side: str  # "Sell to close" | "Buy to close"
    contract: str  # e.g. "66P"
    strike: float
    option_type: str
    expiration: str


class ExitLevelView(BaseModel):
    kind: str
    label: str
    net_price: float | None = None
    pnl_usd: float | None = None
    note: str = ""


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
    # --- detail ---
    expiration: str | None = None
    max_loss_usd: float | None = None
    max_profit_usd: float | None = None
    breakevens: list[float] = Field(default_factory=list)
    time_stop_dte: int | None = None
    legs: list[LegView] = Field(default_factory=list)
    exit_levels: list[ExitLevelView] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SyncResult(BaseModel):
    synced: int
    message: str


def _build_view(t: PaperTrade, r: PositionRisk | None) -> PositionView:
    plan = t.trade_plan
    exps = [lg.expiration for lg in plan.legs]
    expiration = str(min(exps)) if exps else None

    legs = [
        LegView(
            side="Sell to close" if lg.action in _BUY else "Buy to close",
            contract=f"{lg.strike:g}{'C' if lg.option_type.value == 'call' else 'P'}",
            strike=lg.strike,
            option_type=lg.option_type.value,
            expiration=str(lg.expiration),
        )
        for lg in plan.legs
    ]
    exit_levels = []
    if plan.exit_plan is not None:
        exit_levels = [
            ExitLevelView(
                kind=lv.kind, label=lv.label, net_price=lv.net_price,
                pnl_usd=lv.pnl_usd, note=lv.note,
            )
            for lv in plan.exit_plan.levels
        ]

    action = r.action if r else "unmarked"
    dte = r.dte if r else None
    time_stop = plan.risk.time_stop_dte
    warnings: list[str] = []
    if action == "stop":
        warnings.append("Stop hit — close.")
    elif action == "take_profit":
        warnings.append("Take-profit reached — bank it.")
    elif action == "time_stop":
        warnings.append(f"Within {time_stop}-DTE time stop — close or roll.")
    elif dte is not None and time_stop is not None and dte <= time_stop + 2:
        warnings.append(f"Approaching time stop (DTE {dte}).")
    if action == "unmarked":
        warnings.append("No live mark right now — data may be stale.")
    pnl_pct = r.pnl_pct if r else None
    if pnl_pct is not None and -0.5 < pnl_pct <= -0.4:
        warnings.append("Approaching the stop (~-50%).")

    return PositionView(
        symbol=t.symbol,
        strategy=plan.strategy.value,
        contracts=plan.contracts,
        entry_net=t.entry_fill,
        current_net=r.current_net if r else None,
        pnl_usd=r.pnl_usd if r else None,
        pnl_pct=pnl_pct,
        dte=dte,
        action=action,
        note=r.note if r else "",
        expiration=expiration,
        max_loss_usd=plan.risk.max_loss_usd,
        max_profit_usd=plan.risk.max_profit_usd,
        breakevens=list(plan.analytics.breakevens) if plan.analytics else [],
        time_stop_dte=time_stop,
        legs=legs,
        exit_levels=exit_levels,
        warnings=warnings,
    )


@router.get("", response_model=list[PositionView])
async def list_positions() -> list[PositionView]:
    trades = await run_in_threadpool(repository.list_paper_trades, 200)
    open_trades = [t for t in trades if t.status == PaperTradeStatus.OPEN]
    # De-dupe to one row per symbol (latest opened), so re-imports don't double up.
    latest: dict[str, PaperTrade] = {}
    for t in sorted(open_trades, key=lambda x: x.opened_at):
        latest[t.symbol] = t
    open_trades = list(latest.values())

    monitor = Tier4PositionMonitor(chain=registry.options_chain_provider())
    risks = await monitor.run(open_trades)
    by_id = {r.trade_id: r for r in risks}

    rows = [_build_view(t, by_id.get(t.id)) for t in open_trades]
    # Surface risk first: stops/take-profits/expiries above holds.
    order = {"stop": 0, "take_profit": 1, "time_stop": 2, "unmarked": 3, "hold": 4}
    rows.sort(key=lambda x: (order.get(x.action, 5), -(x.pnl_usd or 0)))
    return rows


@router.post("/sync", response_model=SyncResult)
async def sync_positions() -> SyncResult:
    """Pull open option positions from the configured brokerage into tracked
    positions. Best-effort: reports clearly what's missing if it can't."""
    from app.services.position_import import sync_broker_positions

    try:
        n, msg = await sync_broker_positions()
    except Exception as exc:  # noqa: BLE001 - surface the reason to the UI
        raise HTTPException(400, f"Broker sync unavailable: {exc}") from exc
    return SyncResult(synced=n, message=msg)
