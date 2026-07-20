"""Short-duration paper trading, monitoring, and performance.

Opens paper positions from armed candidates (reusing the signed-net paper engine
for fills + slippage + MFE/MAE), monitors them with INTRADAY time-stops (new — the
core engine only has DTE-keyed stops), and reports performance by strategy /
regime / time-of-day / DTE / score band / news+flow confirmation. Realized P&L
feeds `daily_risk_state`, which the Phase-4 entry gates consume — closing the loop.

Nothing here places a live order; every fill is simulated.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.db import repository
from app.domain.enums import CandidateState, ExitReason, PaperTradeStatus
from app.domain.shortduration import ShortDurationCandidate, ShortDurationTrade
from app.logging_config import get_logger
from app.providers import registry
from app.services.paper_engine import check_exit, close_paper_trade, open_paper_trade, update_mark
from app.shortduration.state import advance, transition
from app.tiers.tier4_positions import mark_net_per_share

log = get_logger(__name__)
_ET = ZoneInfo("America/New_York")


def time_of_day_bucket(now: datetime) -> str:
    t = now.astimezone(_ET).time()
    from datetime import time as _t

    if t < _t(10, 30):
        return "first_hour"
    if t < _t(15, 0):
        return "midday"
    if t <= _t(16, 0):
        return "power_hour"
    return "other"


def _advance_to_open(cand: ShortDurationCandidate, now: datetime) -> list:
    """Advance the candidate to OPEN via the paper/research path (ARMED ->
    TRIGGERED -> OPEN, skipping PROPOSED/APPROVED which are the live path)."""
    return advance(cand, CandidateState.OPEN, trigger="paper_open", actor="dashboard",
                   reason="Opened paper trade.", at=now)


async def open_short_duration_paper(
    candidate: ShortDurationCandidate, *, now: datetime | None = None
) -> ShortDurationTrade:
    """Open a simulated position from an armed, tradeable candidate."""
    now = now or datetime.now(UTC)
    plan = candidate.trade_plan
    if plan is None:
        raise ValueError("Candidate has no sized contract to paper-trade.")
    if candidate.state in {CandidateState.CLOSED, CandidateState.REJECTED, CandidateState.EXPIRED}:
        raise ValueError(f"Candidate is {candidate.state.value}; cannot open.")

    entry_mid = round(plan.net_debit / 100.0, 4)  # per-share net debit
    pt = open_paper_trade(plan, scan_id=f"sd:{candidate.id}", entry_mid=entry_mid, now=now)
    await asyncio.to_thread(repository.save_paper_trade, pt)

    ns = candidate.news_score
    flow_conf = None
    if candidate.scorecard and candidate.scorecard.components.get("flow_confidence"):
        flow_conf = candidate.scorecard.components["flow_confidence"].value
    sd_trade = ShortDurationTrade(
        id=uuid.uuid4().hex[:12],
        candidate_id=candidate.id,
        paper_trade_id=pt.id,
        symbol=candidate.symbol,
        dte_category=candidate.dte_category,
        strategy=candidate.strategy,
        direction=candidate.direction,
        regime=candidate.regime,
        entry_score=candidate.score,
        entry_confidence=candidate.confidence,
        news_confirmed=bool(ns and (ns.price_confirmed > 0 or ns.total >= 0.6)),
        flow_confirmed=bool(flow_conf and flow_conf >= 0.4),
        time_of_day=time_of_day_bucket(now),
        opened_at=now,
        entry_net=pt.entry_fill,
        contracts=plan.contracts,
        max_loss_usd=plan.risk.max_loss_usd,
        status="open",
    )
    await asyncio.to_thread(repository.save_short_duration_trade, sd_trade)

    for tr in _advance_to_open(candidate, now):
        await asyncio.to_thread(repository.append_candidate_transition, tr)
    await asyncio.to_thread(repository.save_short_duration_candidate, candidate)

    log.info("sd_paper_open", symbol=candidate.symbol, trade_id=sd_trade.id)
    return sd_trade


def _intraday_exit(dte: int | None, time_stop_dte: int | None, now: datetime) -> ExitReason | None:
    """Short-duration time-stops the core engine doesn't do:
    - past expiry -> EXPIRY;
    - same-day (0DTE) -> force-close by the 15:45 ET review time so it never
      rides into the settlement/pin risk of the final minutes;
    - otherwise close when DTE drops to the plan's time stop (1-5DTE).
    """
    from datetime import time as _t

    if dte is None:
        return None
    if dte < 0:
        return ExitReason.EXPIRY
    if dte == 0:
        return ExitReason.TIME_STOP if now.astimezone(_ET).time() >= _t(15, 45) else None
    if time_stop_dte is not None and time_stop_dte >= 1 and dte <= time_stop_dte:
        return ExitReason.TIME_STOP
    return None


async def monitor_short_duration_positions(*, now: datetime | None = None) -> list[ShortDurationTrade]:
    """Mark each open short-duration paper position from the live chain, apply
    exits (profit / stop / intraday time-stop / expiry), and close when hit."""
    now = now or datetime.now(UTC)
    open_trades = await asyncio.to_thread(repository.list_short_duration_trades, status="open")
    chain_provider = registry.options_chain_provider()
    updated: list[ShortDurationTrade] = []

    for sd in open_trades:
        pt = await asyncio.to_thread(repository.get_paper_trade, sd.paper_trade_id)
        if pt is None or pt.status != PaperTradeStatus.OPEN:
            continue
        plan = pt.trade_plan
        exps = sorted({lg.expiration for lg in plan.legs})
        try:
            chain = await chain_provider.get_option_chain_for_expirations(sd.symbol, exps)
        except Exception as exc:  # noqa: BLE001 - can't mark this pass; leave open
            log.warning("sd_monitor_chain_failed", symbol=sd.symbol, error=str(exc))
            continue
        net = mark_net_per_share(plan, chain)
        if net is None:
            sd.current_net = None
            sd.unrealized_pnl_usd = None
            await asyncio.to_thread(repository.save_short_duration_trade, sd)
            updated.append(sd)
            continue

        pt = update_mark(pt, net)
        today = now.astimezone(_ET).date()
        dte = (min(exps) - today).days if exps else None
        reason = check_exit(pt, net) or _intraday_exit(dte, plan.risk.time_stop_dte, now)

        sd.current_net = net
        sd.unrealized_pnl_usd = pt.mark_pnl_usd(net)
        sd.mfe_usd, sd.mae_usd = pt.mfe_usd, pt.mae_usd

        if reason is not None:
            pt = close_paper_trade(pt, exit_mid=net, reason=reason, now=now)
            await asyncio.to_thread(repository.save_paper_trade, pt)
            sd.status = "closed"
            sd.exit_net = pt.exit_fill
            sd.realized_pnl_usd = pt.realized_pnl_usd
            sd.exit_reason = reason.value
            sd.closed_at = now
            cand = await asyncio.to_thread(repository.get_short_duration_candidate, sd.candidate_id)
            if cand is not None and cand.state in {CandidateState.OPEN, CandidateState.MANAGING}:
                tr = transition(cand, CandidateState.CLOSED, trigger=f"exit:{reason.value}",
                                actor="system", reason=f"Closed ({reason.value}).", at=now)
                await asyncio.to_thread(repository.append_candidate_transition, tr)
                await asyncio.to_thread(repository.save_short_duration_candidate, cand)
        await asyncio.to_thread(repository.save_short_duration_trade, sd)
        updated.append(sd)

    log.info("sd_monitor", open=len(open_trades), acted=sum(1 for t in updated if t.status == "closed"))
    return updated


def daily_risk_state(now: datetime | None = None):
    """Today's realized P&L, trailing consecutive losses, and open count — the
    input the Phase-4 daily-loss / halt gates consume."""
    from app.shortduration.risk import DailyRiskState

    now = now or datetime.now(UTC)
    trades = repository.list_short_duration_trades(limit=1000)
    today = now.astimezone(_ET).date()
    closed_today = sorted(
        [t for t in trades if t.closed_at and t.closed_at.astimezone(_ET).date() == today],
        key=lambda t: t.closed_at,  # type: ignore[arg-type,return-value]
    )
    realized = round(sum(t.realized_pnl_usd or 0.0 for t in closed_today), 2)
    consec = 0
    for t in reversed(closed_today):
        if (t.realized_pnl_usd or 0.0) < 0:
            consec += 1
        else:
            break
    open_count = sum(1 for t in trades if t.status == "open")
    return DailyRiskState(realized_pnl_usd=realized, consecutive_losses=consec, open_positions=open_count)


# --- Performance -------------------------------------------------------------
def _stats(trades: list[ShortDurationTrade]) -> dict:
    decided = [t for t in trades if t.realized_pnl_usd is not None]
    if not decided:
        return {"trades": 0}
    pnls = [t.realized_pnl_usd or 0.0 for t in decided]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    return {
        "trades": len(decided),
        "win_rate": round(len(wins) / len(decided), 3),
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "expectancy": round(sum(pnls) / len(decided), 2),
        "total_pnl": round(sum(pnls), 2),
    }


def _group(trades: list[ShortDurationTrade], key) -> dict[str, dict]:
    buckets: dict[str, list[ShortDurationTrade]] = defaultdict(list)
    for t in trades:
        buckets[str(key(t))].append(t)
    return {k: _stats(v) for k, v in sorted(buckets.items())}


def short_duration_performance() -> dict:
    """Aggregate + sliced performance across closed short-duration paper trades."""
    trades = repository.list_short_duration_trades(status="closed", limit=2000)
    return {
        "overall": _stats(trades),
        "open_positions": len(repository.list_short_duration_trades(status="open")),
        "by_dte": _group(trades, lambda t: t.dte_category.value),
        "by_strategy": _group(trades, lambda t: t.strategy.value if t.strategy else "—"),
        "by_symbol": _group(trades, lambda t: t.symbol),
        "by_regime": _group(trades, lambda t: t.regime.value if t.regime else "—"),
        "by_time_of_day": _group(trades, lambda t: t.time_of_day or "—"),
        "by_score_band": _group(trades, lambda t: t.score_band),
        "by_news_confirmed": _group(trades, lambda t: "news" if t.news_confirmed else "no-news"),
        "by_flow_confirmed": _group(trades, lambda t: "flow" if t.flow_confirmed else "no-flow"),
    }
