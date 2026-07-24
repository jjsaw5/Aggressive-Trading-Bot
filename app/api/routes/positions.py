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
from app.domain.enums import Direction, ExitReason, OptionAction, OptionType, PaperTradeStatus
from app.domain.shortduration import DirectionalThesis
from app.domain.trades import PaperTrade
from app.logging_config import get_logger
from app.providers import registry
from app.quant.analytics import structure_breakevens
from app.quant.probability import probability_of_profit, what_has_to_happen
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
    # Market-implied odds + a plain-English "what has to happen" line (informational).
    probability_of_profit: float | None = None
    what_has_to_happen: str = ""
    # Reversal-risk read on the OPEN position (deterministic, informational): is
    # today's move against the trade, is price near its invalidation, etc.
    thesis: DirectionalThesis | None = None


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
    t: PaperTrade, r: PositionRisk | None, earnings: date | None = None,
    thesis: DirectionalThesis | None = None,
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
    if not breakevens and plan.legs:
        # Every supported structure has a computable breakeven; none means this
        # stored record is malformed (its strategy label doesn't match its legs).
        warnings.append(
            "This record looks malformed — its strategy label doesn't match its legs, "
            "so breakeven/POP can't be computed. Delete it and re-add (the quick-add "
            "line is the easiest way)."
        )

    # Market-implied odds + the concrete "what has to happen" line — from the live
    # spot, the nearest break-even, the marked IV, and days to expiry.
    bullish = plan.direction == Direction.BULLISH
    spot = r.underlying_price if r else None
    pop = None
    what_has = ""
    if spot and breakevens:
        be = min(breakevens, key=lambda b: abs(b - spot))
        days = dte if dte is not None else None
        if r and r.atm_iv and days is not None:
            pop = probability_of_profit(
                spot=spot, breakeven=be, iv=r.atm_iv, days=float(days), bullish=bullish
            )
        what_has = what_has_to_happen(
            symbol=t.symbol, spot=spot, breakeven=be, days=days, bullish=bullish
        )
    if thesis is not None and thesis.reversal_risk in ("elevated", "high"):
        warnings.append(f"Reversal risk {thesis.reversal_risk.upper()} — see thesis.")

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
        probability_of_profit=pop,
        what_has_to_happen=what_has,
        thesis=thesis,
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


async def _theses_by_trade(trades: list[PaperTrade]) -> dict[str, DirectionalThesis]:
    """A deterministic reversal-risk read per open position: fetch each symbol's
    daily history + quote once, then build the same DirectionalThesis the scanner
    uses — so a held trade shows a counter-trend day / near-invalidation warning
    while you're carrying it. Best-effort; a provider miss just omits the thesis.

    Structural (wrong-instrument) warnings are intentionally left to the entry-time
    thesis: the position view already surfaces earnings-before-expiry on its own,
    and horizon-mismatch is an entry decision, not a hold signal."""
    from datetime import UTC, datetime

    from app.domain.enums import DTECategory, ShortDurationRegime, ShortDurationStrategy
    from app.domain.shortduration import ShortDurationRegimeState
    from app.shortduration.strategies.base import SetupContext, StrategyDetection
    from app.shortduration.thesis import build_directional_thesis

    now = datetime.now(UTC)
    market = registry.market_data_provider()
    syms = sorted({t.symbol for t in trades})
    if not syms:
        return {}

    async def _fetch(sym: str):
        try:
            daily = await market.get_price_history(sym, lookback_days=252)
        except Exception as exc:  # noqa: BLE001 - thesis is optional context
            log.warning("positions_thesis_daily_failed", symbol=sym, error=str(exc))
            daily = None
        try:
            quote = await market.get_quote(sym)
        except Exception:  # noqa: BLE001
            quote = None
        return sym, daily, quote

    fetched = {sym: (daily, quote) for sym, daily, quote in await asyncio.gather(*(_fetch(s) for s in syms))}

    regime = ShortDurationRegimeState(
        regime=ShortDurationRegime.RANGE_BOUND, confidence=0.0, allow_new_trades=True, as_of=now
    )
    out: dict[str, DirectionalThesis] = {}
    for t in trades:
        daily, quote = fetched.get(t.symbol, (None, None))
        if daily is None:
            continue
        chg = None
        if quote and quote.prev_close:
            chg = round((quote.price - quote.prev_close) / quote.prev_close * 100, 3)
        ctx = SetupContext(symbol=t.symbol, now=now, regime=regime, daily=daily, quote=quote, change_pct=chg)
        # A non-swing strategy + no earnings in ctx keeps this to the reversal-risk /
        # daily read — the position view owns the earnings and instrument concerns.
        det = StrategyDetection(
            strategy=ShortDurationStrategy.CATALYST_CONTINUATION, dte_category=DTECategory.SHORT_DTE,
            direction=t.trade_plan.direction, setup_score=0.0, entry_trigger="", invalidation="",
        )
        try:
            out[t.id] = build_directional_thesis(ctx, det)
        except Exception as exc:  # noqa: BLE001
            log.warning("positions_thesis_build_failed", symbol=t.symbol, error=str(exc))
    return out


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
    theses = await _theses_by_trade(open_trades)

    # Per-row isolation: one malformed stored record must degrade to a visible
    # warning row the user can delete — never 500 the whole board.
    rows = []
    for t in open_trades:
        try:
            rows.append(_build_view(t, by_id.get(t.id), earnings.get(t.symbol), theses.get(t.id)))
        except Exception as exc:  # noqa: BLE001 — isolate the poison row
            log.warning("position_view_failed", trade_id=t.id, symbol=t.symbol, error=str(exc))
            rows.append(PositionView(
                id=t.id, source=t.scan_id or "", symbol=t.symbol,
                strategy=t.trade_plan.strategy.display_name if t.trade_plan else "unknown",
                contracts=t.trade_plan.contracts if t.trade_plan else 1,
                entry_net=t.entry_fill, action="unmarked",
                warnings=[
                    "This record is malformed (its strategy label doesn't match its legs) "
                    "and can't be fully displayed. Delete it and re-add — the quick-add "
                    "line is the easiest way."
                ],
            ))
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


class QuickAddRequest(BaseModel):
    line: str  # e.g. "TSLA 370/365p 7/24 @2.45 x1"
    opened_at: datetime | None = None


def _warehouse_live(trade: PaperTrade) -> None:
    """Freeze a live import into the decision warehouse so the calibration
    scorecard grades real trades. Best-effort — a warehouse hiccup must never
    block an import."""
    try:
        from app.analytics.snapshots import snapshot_from_live_trade
        snap = snapshot_from_live_trade(trade)
        if snap is not None:
            repository.save_snapshots([snap])
    except Exception as exc:  # noqa: BLE001
        log.warning("live_warehouse_failed", trade_id=trade.id, error=str(exc))


@router.post("/quick-add")
async def quick_add_position(req: QuickAddRequest) -> dict:
    """One-line position entry: 'TSLA 370/365p 7/24 @2.45 x1'. First strike =
    the leg you bought, second = the leg you sold; net + is a debit, − a credit;
    M/D dates roll forward to the next occurrence. Tracked and warehoused like
    any other live position."""
    from app.services.position_import import build_tracked_trade, parse_trade_line

    try:
        symbol, legs, net, _qty = parse_trade_line(req.line)
        trade = build_tracked_trade(
            symbol, legs, opened_at=req.opened_at, source="manual", net_per_share=net
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    await run_in_threadpool(repository.save_paper_trade, trade)
    await run_in_threadpool(_warehouse_live, trade)
    legs_desc = " / ".join(
        f"{'long' if lg.action in _BUY else 'short'} {lg.strike:g}{lg.option_type.value[0].upper()}"
        for lg in trade.trade_plan.legs
    )
    return {
        "id": trade.id, "symbol": trade.symbol,
        "strategy": trade.trade_plan.strategy.display_name,
        "parsed": f"{legs_desc} exp {trade.trade_plan.legs[0].expiration} "
                  f"net {trade.trade_plan.net_debit / 100:+.2f}/sh x{trade.trade_plan.contracts}",
        "max_loss_usd": trade.trade_plan.risk.max_loss_usd,
        "message": f"Added {trade.trade_plan.strategy.display_name} on {trade.symbol}. Now monitored.",
    }


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
    await run_in_threadpool(_warehouse_live, trade)
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
    # Grade the close: a real fill is ground truth. Ensure the decision snapshot
    # exists (positions imported before warehousing existed get one lazily), then
    # record the outcome so the calibration scorecard tracks live-trade success.
    await run_in_threadpool(_record_live_close, t, now)
    return _closed_view(t)


def _record_live_close(t: PaperTrade, closed_at: datetime) -> None:
    """Write the live_close DecisionOutcome for a closed real position.
    Best-effort — grading must never block a close."""
    try:
        from app.analytics.snapshots import snapshot_from_live_trade
        from app.domain.outcomes import DecisionOutcome, OutcomeResult

        decision_id = f"live:{t.id}"
        if repository.get_snapshot(decision_id) is None:
            snap = snapshot_from_live_trade(t)
            if snap is None:
                return
            repository.save_snapshots([snap])
        pnl = t.realized_pnl_usd or 0.0
        result = (
            OutcomeResult.WIN if pnl > 0
            else OutcomeResult.LOSS if pnl < 0
            else OutcomeResult.SCRATCH
        )
        elapsed = max(0, (closed_at.date() - t.opened_at.date()).days)
        repository.save_outcome(DecisionOutcome(
            decision_id=decision_id, symbol=t.symbol, horizon_label="trade_close",
            resolved_at=closed_at, elapsed_days=elapsed, result=result,
            realized_pnl_usd=pnl, outcome_source="live_close",
            note=t.exit_note or "",
        ))
    except Exception as exc:  # noqa: BLE001
        log.warning("live_close_grade_failed", trade_id=t.id, error=str(exc))


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
