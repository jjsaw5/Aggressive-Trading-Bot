"""Live positions view — every open tracked position marked by Tier 4.

Runs the Tier-4 monitor over open positions and returns, per position: P&L, the
mechanical exit action, the full exit plan (a constant reminder of where to
close), the close-order ticket (legs reversed), risk economics, and warnings.
Also supports syncing positions from the configured brokerage. Never places
orders.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.db import repository
from app.domain.enums import ExitReason, OptionAction, OptionType, PaperTradeStatus
from app.domain.trades import PaperTrade
from app.logging_config import get_logger
from app.providers import registry
from app.quant.analytics import structure_breakevens
from app.tiers.models import PositionRisk
from app.tiers.tier4_positions import Tier4PositionMonitor

log = get_logger(__name__)

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
    id: str = ""
    source: str = ""
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
    # --- live risk profile ---
    underlying_price: float | None = None
    net_delta: float | None = None  # shares-equivalent exposure
    net_theta: float | None = None  # $/day decay
    breakeven_distance_pct: float | None = None  # signed move to nearest breakeven
    earnings_date: str | None = None
    earnings_before_expiry: bool = False


class SyncResult(BaseModel):
    synced: int
    message: str


def _structure_sig(t: PaperTrade) -> tuple:
    legs = tuple(sorted(
        (lg.strike, lg.option_type.value, lg.action.value, str(lg.expiration))
        for lg in t.trade_plan.legs
    ))
    return (t.symbol, legs)


# --- Manual position entry (no broker connection) ---------------------------
class ImportLegRequest(BaseModel):
    strike: float = Field(gt=0)
    option_type: str  # "call" | "put"
    is_long: bool  # bought (long) vs sold (short)
    quantity: int = Field(default=1, ge=1)
    # Per-share premium. Optional when the whole-structure `net_debit_per_share` is
    # given (the easy path for a spread — you just enter your average cost).
    entry_price_per_share: float | None = None
    expiration: date


class ImportPositionRequest(BaseModel):
    symbol: str
    legs: list[ImportLegRequest] = Field(min_length=1)
    opened_at: datetime | None = None
    # The structure's net cost per share the way a broker quotes it: a debit is
    # positive (you paid), a credit is negative (you received). When set, per-leg
    # prices are ignored — no reverse-engineering leg fills from a net.
    net_debit_per_share: float | None = None


class ClosePositionRequest(BaseModel):
    # The net per-share value you closed the structure at (same sign as entry: a
    # debit structure is positive). Realized P&L = (exit − entry) × 100 × contracts.
    exit_price_per_share: float
    reason: str | None = None
    closed_at: datetime | None = None


class ClosedPositionView(BaseModel):
    id: str
    symbol: str
    strategy: str
    direction: str
    contracts: int
    entry_net: float
    exit_net: float | None = None
    realized_pnl_usd: float | None = None
    exit_reason: str | None = None
    exit_note: str = ""
    opened_at: datetime
    closed_at: datetime | None = None
    hold_days: float | None = None
    source: str = ""


def _closed_view(t: PaperTrade) -> ClosedPositionView:
    hold = None
    if t.closed_at is not None:
        hold = round((t.closed_at - t.opened_at).total_seconds() / 86400.0, 2)
    return ClosedPositionView(
        id=t.id, symbol=t.symbol, strategy=t.trade_plan.strategy.value,
        direction=t.trade_plan.direction.value, contracts=t.trade_plan.contracts,
        entry_net=t.entry_fill, exit_net=t.exit_fill, realized_pnl_usd=t.realized_pnl_usd,
        exit_reason=t.exit_reason.value if t.exit_reason else None, exit_note=t.exit_note,
        opened_at=t.opened_at, closed_at=t.closed_at, hold_days=hold, source=t.scan_id,
    )


def _build_view(
    t: PaperTrade, r: PositionRisk | None, earnings: date | None = None
) -> PositionView:
    plan = t.trade_plan
    exps = [lg.expiration for lg in plan.legs]
    expiration = str(min(exps)) if exps else None
    min_exp = min(exps) if exps else None

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

    # Earnings before expiry: a binary IV-crush / gap risk on a defined-risk hold.
    earnings_before = bool(earnings and min_exp and earnings <= min_exp)
    if earnings_before:
        warnings.append(f"Earnings {earnings} — before expiry. Expect an IV-crush gap.")

    # Breakevens: live from the chain-marked risk if present, else structural.
    breakevens = list(plan.analytics.breakevens) if plan.analytics else []
    if not breakevens:
        breakevens = structure_breakevens(plan)

    return PositionView(
        id=t.id,
        source=t.scan_id,
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
        breakevens=breakevens,
        time_stop_dte=time_stop,
        legs=legs,
        exit_levels=exit_levels,
        warnings=warnings,
        underlying_price=r.underlying_price if r else None,
        net_delta=r.net_delta if r else None,
        net_theta=r.net_theta if r else None,
        breakeven_distance_pct=r.breakeven_distance_pct if r else None,
        earnings_date=str(earnings) if earnings else None,
        earnings_before_expiry=earnings_before,
    )


async def _earnings_by_symbol(symbols: list[str]) -> dict[str, date]:
    """Next earnings date per symbol, best-effort. Never fails the page: a
    provider hiccup just means no earnings flag for that symbol."""
    uniq = sorted(set(symbols))
    if not uniq:
        return {}
    cal = registry.calendar_provider()

    async def one(sym: str) -> tuple[str, date | None]:
        try:
            ev = await cal.get_earnings(sym)
            return sym, (ev.report_date if ev else None)
        except Exception as exc:  # noqa: BLE001 - earnings are optional context
            log.warning("positions_earnings_failed", symbol=sym, error=str(exc))
            return sym, None

    results = await asyncio.gather(*(one(s) for s in uniq))
    return {sym: d for sym, d in results if d is not None}


@router.get("", response_model=list[PositionView])
async def list_positions() -> list[PositionView]:
    trades = await run_in_threadpool(repository.list_paper_trades, 200)
    open_trades = [t for t in trades if t.status == PaperTradeStatus.OPEN]
    # De-dupe by STRUCTURE (symbol + legs), not bare symbol, so distinct positions on
    # the same underlying both show — a re-import of the *same* structure still
    # collapses to the latest.
    latest: dict[tuple, PaperTrade] = {}
    for t in sorted(open_trades, key=lambda x: x.opened_at):
        latest[_structure_sig(t)] = t
    open_trades = list(latest.values())

    monitor = Tier4PositionMonitor(chain=registry.options_chain_provider())
    risks = await monitor.run(open_trades)
    by_id = {r.trade_id: r for r in risks}

    earnings = await _earnings_by_symbol([t.symbol for t in open_trades])

    rows = [_build_view(t, by_id.get(t.id), earnings.get(t.symbol)) for t in open_trades]
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


@router.post("/import")
async def import_position(req: ImportPositionRequest) -> dict:
    """Manually add an open position (no broker connection). It becomes a tracked
    position marked live from the FMP chain, exactly like a synced one."""
    from app.services.position_import import ImportedLeg, build_tracked_trade

    net = req.net_debit_per_share
    try:
        legs = []
        for lg in req.legs:
            px = lg.entry_price_per_share
            if net is None and (px is None or px <= 0):
                raise ValueError("each leg needs an entry price, or give a net cost for the spread")
            legs.append(ImportedLeg(
                strike=lg.strike, option_type=OptionType(lg.option_type.lower()),
                is_long=lg.is_long, quantity=lg.quantity,
                entry_price_per_share=px or 0.0, expiration=lg.expiration,
            ))
        trade = build_tracked_trade(
            req.symbol, legs, opened_at=req.opened_at, source="manual", net_per_share=net
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, f"Invalid position: {exc}") from exc
    await run_in_threadpool(repository.save_paper_trade, trade)
    return {
        "id": trade.id, "symbol": trade.symbol,
        "strategy": trade.trade_plan.strategy.display_name,
        "contracts": trade.trade_plan.contracts, "entry_net": trade.entry_fill,
        "max_loss_usd": trade.trade_plan.risk.max_loss_usd,
        "message": f"Added {trade.trade_plan.strategy.display_name} on {trade.symbol}. Now monitored.",
    }


@router.post("/{trade_id}/close")
async def close_position(trade_id: str, req: ClosePositionRequest) -> ClosedPositionView:
    """Mark a tracked position closed at the net you exited at. Retained (status
    CLOSED) for the history/review board and the performance analytics."""
    t = await run_in_threadpool(repository.get_paper_trade, trade_id)
    if t is None:
        raise HTTPException(404, "Position not found.")
    if t.status == PaperTradeStatus.CLOSED:
        raise HTTPException(409, "Position is already closed.")
    now = req.closed_at or datetime.now(UTC)
    # Manual close = the REAL fill you got, so no simulated slippage.
    t.status = PaperTradeStatus.CLOSED
    t.closed_at = now
    t.exit_fill = req.exit_price_per_share
    t.exit_slippage = 0.0
    t.exit_reason = ExitReason.MANUAL
    t.exit_note = req.reason or ""
    t.realized_pnl_usd = round(
        (req.exit_price_per_share - t.entry_fill) * t.trade_plan.contracts * 100, 2
    )
    await run_in_threadpool(repository.save_paper_trade, t)
    return _closed_view(t)


@router.delete("/{trade_id}")
async def delete_position(trade_id: str) -> dict:
    """Permanently delete a tracked position (open or closed) — for purging bad
    manually-entered data. This removes it entirely, unlike close which retains it."""
    ok = await run_in_threadpool(repository.delete_paper_trade, trade_id)
    if not ok:
        raise HTTPException(404, "Position not found.")
    return {"deleted": True, "id": trade_id}


@router.get("/history", response_model=list[ClosedPositionView])
async def positions_history(limit: int = 200, source: str | None = None) -> list[ClosedPositionView]:
    """Closed positions, newest first — retained for review and model tuning.
    Optionally filter by `source` (e.g. `manual`)."""
    trades = await run_in_threadpool(repository.list_paper_trades, limit)
    closed = [t for t in trades if t.status == PaperTradeStatus.CLOSED]
    if source:
        closed = [t for t in closed if t.scan_id == source]
    closed.sort(key=lambda t: t.closed_at or t.opened_at, reverse=True)
    return [_closed_view(t) for t in closed]
