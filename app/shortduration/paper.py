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
from app.services.paper_engine import (
    SlippageModel,
    check_exit,
    close_paper_trade,
    open_paper_trade,
    update_mark,
)
from app.shortduration.state import advance, transition
from app.tiers.tier4_positions import mark_net_per_share, structure_spread

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


_NOT_EXEC_REASON = {
    "account_cap_exhausted": "Open risk already consumes the account cap — no room for this trade.",
    "single_contract_exceeds_trade_cap": "One contract exceeds the per-trade risk cap for this account.",
    "invalid_per_contract_risk": "Per-contract risk could not be determined.",
}


async def _executable_at_entry(plan, dte, now: datetime) -> tuple[bool, str]:
    """Would this sized structure fit the REAL account at entry (Book B), or is it
    a signal-validation-only trade (Book A)? Re-sizes the plan's per-contract risk
    under the constrained account policy — independent of paper-unconstrained mode,
    which lifts the cap for the signal book. Best-effort: on any error, treat as
    executable so the account book is never silently starved by a transient failure."""
    from app.risk.position_sizing import size_by_defined_risk
    from app.shortduration.risk import short_duration_policy

    try:
        contracts = max(1, int(plan.contracts or 1))
        per_contract = (plan.risk.max_loss_usd or 0.0) / contracts
        if per_contract <= 0:
            return True, ""
        account = await _account_state_for_paper(now)
        policy = short_duration_policy(dte, equity=account.equity_usd, constrained=True)
        sizing = size_by_defined_risk(
            per_contract, policy, open_risk_usd=account.committed_risk_usd
        )
        if sizing.is_tradeable:
            return True, ""
        reason = sizing.capped_reason or "not_sizeable"
        return False, _NOT_EXEC_REASON.get(reason, reason)
    except Exception as exc:  # noqa: BLE001 - never block a paper open on a sizing hiccup
        log.warning("sd_executable_check_failed", error=str(exc))
        return True, ""


async def _account_state_for_paper(now: datetime):
    return await registry.account_state_provider().get_account_state(now=now)


def _advance_to_open(cand: ShortDurationCandidate, now: datetime) -> list:
    """Advance the candidate to OPEN via the paper/research path (ARMED ->
    TRIGGERED -> OPEN, skipping PROPOSED/APPROVED which are the live path)."""
    return advance(cand, CandidateState.OPEN, trigger="paper_open", actor="dashboard",
                   reason="Opened paper trade.", at=now)


async def _entry_spread(plan, symbol: str, now: datetime) -> float:
    """Best-effort real bid/ask spread of the structure at entry, so the paper fill
    crosses realistic slippage rather than only the per-share floor. A chain miss
    degrades to 0.0 (floor still applies) — never blocks the open."""
    exps = sorted({lg.expiration for lg in plan.legs})
    try:
        chain = await registry.options_chain_provider().get_option_chain_for_expirations(symbol, exps)
    except Exception as exc:  # noqa: BLE001 - a chain miss must not block opening
        log.warning("sd_entry_spread_chain_failed", symbol=symbol, error=str(exc))
        return 0.0
    return structure_spread(plan, chain)


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
    entry_spread = await _entry_spread(plan, candidate.symbol, now)
    pt = open_paper_trade(
        plan, scan_id=f"sd:{candidate.id}", entry_mid=entry_mid,
        entry_spread=entry_spread, now=now,
    )
    await asyncio.to_thread(repository.save_paper_trade, pt)

    ns = candidate.news_score
    flow_conf = None
    if candidate.scorecard and candidate.scorecard.components.get("flow_confidence"):
        flow_conf = candidate.scorecard.components["flow_confidence"].value
    executable, why_not = await _executable_at_entry(plan, candidate.dte_category, now)
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
        executable_at_entry=executable,
        not_executable_reason=why_not,
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
            # The wrapped paper trade is gone (record purged) or was closed
            # outside this monitor — mirror that onto the SD row instead of
            # leaving an orphan OPEN on the board forever.
            sd.status = "closed"
            sd.closed_at = pt.closed_at if pt is not None else now
            sd.exit_net = pt.exit_fill if pt is not None else None
            sd.realized_pnl_usd = pt.realized_pnl_usd if pt is not None else None
            sd.exit_reason = (pt.exit_reason.value if pt is not None and pt.exit_reason
                              else "orphaned")
            sd.unrealized_pnl_usd = None
            await asyncio.to_thread(repository.save_short_duration_trade, sd)
            cand = await asyncio.to_thread(repository.get_short_duration_candidate, sd.candidate_id)
            if cand is not None and cand.state in {CandidateState.OPEN, CandidateState.MANAGING}:
                tr = transition(cand, CandidateState.CLOSED, trigger="exit:orphaned",
                                actor="system",
                                reason="Underlying paper trade closed or purged.", at=now)
                await asyncio.to_thread(repository.append_candidate_transition, tr)
                await asyncio.to_thread(repository.save_short_duration_candidate, cand)
            updated.append(sd)
            continue
        plan = pt.trade_plan
        exps = sorted({lg.expiration for lg in plan.legs})

        # A structure held past expiration can never be marked from the live
        # chain (the expiry no longer exists there) — settle it at the intrinsic
        # value of the underlying's expiry-day close instead of leaving it OPEN
        # forever on the board.
        if exps and max(exps) < now.astimezone(_ET).date():
            settled = await _settle_expired(sd, pt, plan, exps[-1])
            if settled is not None:
                updated.append(settled)
            continue
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
            # Cross the real bid/ask spread on the exit fill too (chain in hand).
            pt = close_paper_trade(
                pt, exit_mid=net, reason=reason, exit_spread=structure_spread(plan, chain), now=now,
            )
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


async def _settle_expired(
    sd: ShortDurationTrade, pt, plan, expiry,
) -> ShortDurationTrade | None:
    """Close a paper position held past expiration at intrinsic value computed
    from the underlying's close on expiry day. Timestamped AT expiry (4pm ET),
    so the history calendar books it on the day it actually died. Returns the
    updated trade, or None when the expiry-day close is unavailable (position
    stays open; a later monitor pass retries)."""
    from datetime import time as dt_time

    md = registry.market_data_provider()
    lookback = (datetime.now(UTC).date() - expiry).days + 10
    try:
        hist = await md.get_price_history(sd.symbol, lookback_days=max(lookback, 15))
    except Exception as exc:  # noqa: BLE001 — can't settle this pass
        log.warning("sd_settle_history_failed", symbol=sd.symbol, error=str(exc))
        return None
    close = next((c.close for c in reversed(hist.candles)
                  if c.ts.astimezone(_ET).date() <= expiry), None)
    if close is None:
        log.warning("sd_settle_no_close", symbol=sd.symbol, expiry=str(expiry))
        return None

    net = 0.0
    for lg in plan.legs:
        intrinsic = (max(0.0, close - lg.strike) if lg.option_type.value == "call"
                     else max(0.0, lg.strike - close))
        net += intrinsic if lg.action.value == "buy_to_open" else -intrinsic
    net = round(net, 4)

    at = datetime.combine(expiry, dt_time(20, 0), tzinfo=UTC)  # 4pm ET (EDT)
    pt = update_mark(pt, net)
    # Settlement is exact intrinsic — there is no market left to cross, so no
    # slippage floor applies (unlike a traded exit).
    no_slip = SlippageModel(spread_fraction=0.0, min_slippage_per_share=0.0)
    pt = close_paper_trade(pt, exit_mid=net, reason=ExitReason.EXPIRY,
                           exit_spread=0.0, slippage=no_slip, now=at)
    await asyncio.to_thread(repository.save_paper_trade, pt)
    sd.status = "closed"
    sd.current_net = net
    sd.exit_net = pt.exit_fill
    sd.realized_pnl_usd = pt.realized_pnl_usd
    sd.unrealized_pnl_usd = None
    sd.exit_reason = ExitReason.EXPIRY.value
    sd.closed_at = at
    await asyncio.to_thread(repository.save_short_duration_trade, sd)
    cand = await asyncio.to_thread(repository.get_short_duration_candidate, sd.candidate_id)
    if cand is not None and cand.state in {CandidateState.OPEN, CandidateState.MANAGING}:
        tr = transition(cand, CandidateState.CLOSED, trigger="exit:expiry", actor="system",
                        reason=f"Settled at expiry ({expiry}) intrinsic {net:+.2f}.", at=at)
        await asyncio.to_thread(repository.append_candidate_transition, tr)
        await asyncio.to_thread(repository.save_short_duration_candidate, cand)
    log.info("sd_settled_at_expiry", symbol=sd.symbol, expiry=str(expiry),
             settle_net=net, pnl=pt.realized_pnl_usd)
    return sd


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
    open_trades = [t for t in trades if t.status == "open"]
    open_book = [
        (t.symbol, t.direction.value if hasattr(t.direction, "value") else str(t.direction))
        for t in open_trades
    ]
    return DailyRiskState(
        realized_pnl_usd=realized, consecutive_losses=consec,
        open_positions=len(open_trades), open_book=open_book,
    )


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


def _sliced(trades: list[ShortDurationTrade]) -> dict:
    """The standard overall + sliced breakdown for one set of trades."""
    return {
        "overall": _stats(trades),
        "by_dte": _group(trades, lambda t: t.dte_category.value),
        "by_strategy": _group(trades, lambda t: t.strategy.value if t.strategy else "—"),
        "by_symbol": _group(trades, lambda t: t.symbol),
        "by_regime": _group(trades, lambda t: t.regime.value if t.regime else "—"),
        "by_time_of_day": _group(trades, lambda t: t.time_of_day or "—"),
        "by_score_band": _group(trades, lambda t: t.score_band),
        "by_news_confirmed": _group(trades, lambda t: "news" if t.news_confirmed else "no-news"),
        "by_flow_confirmed": _group(trades, lambda t: "flow" if t.flow_confirmed else "no-flow"),
    }


def _opportunity_loss(closed: list[ShortDurationTrade]) -> dict:
    """What the small account leaves on the table: the P&L difference between the
    full signal book (A) and the account-executable subset (B), plus the biggest
    non-executable winners the account could not take."""
    decided = [t for t in closed if t.realized_pnl_usd is not None]
    book_a_pnl = round(sum(t.realized_pnl_usd or 0.0 for t in decided), 2)
    exec_decided = [t for t in decided if t.executable_at_entry]
    book_b_pnl = round(sum(t.realized_pnl_usd or 0.0 for t in exec_decided), 2)
    missed = [t for t in decided if not t.executable_at_entry]
    top_missed = sorted(missed, key=lambda t: t.realized_pnl_usd or 0.0, reverse=True)[:5]
    reasons: dict[str, int] = defaultdict(int)
    for t in missed:
        reasons[t.not_executable_reason or "—"] += 1
    return {
        "signals_decided": len(decided),
        "executable_decided": len(exec_decided),
        "not_executable_decided": len(missed),
        "book_a_total_pnl": book_a_pnl,       # every signal, uniformly validated
        "book_b_total_pnl": book_b_pnl,       # only what the account could take
        "left_on_table_pnl": round(book_a_pnl - book_b_pnl, 2),
        "not_executable_reasons": dict(reasons),
        "top_missed": [
            {"symbol": t.symbol, "strategy": t.strategy.value if t.strategy else "—",
             "pnl_usd": t.realized_pnl_usd, "reason": t.not_executable_reason}
            for t in top_missed
        ],
    }


def short_duration_performance(book: str | None = None) -> dict:
    """Aggregate + sliced performance across closed short-duration paper trades,
    split into two books:

    - **Book A** (signal-validation): every opened setup — measures raw signal edge.
    - **Book B** (account-executable): the subset that fit the real account at entry.

    ``book`` selects one book's breakdown ("A"/"B"); omitted returns both plus the
    opportunity-loss analytics (the edge the small account leaves on the table)."""
    closed = repository.list_short_duration_trades(status="closed", limit=2000)
    open_trades = repository.list_short_duration_trades(status="open")
    book_a = closed
    book_b = [t for t in closed if t.executable_at_entry]

    sel = (book or "").upper()
    if sel == "A":
        return {"book": "A", "open_positions": len(open_trades), **_sliced(book_a)}
    if sel == "B":
        return {"book": "B", "open_positions": sum(1 for t in open_trades if t.executable_at_entry),
                **_sliced(book_b)}
    # Default: the flat Book-A shape (all closed setups — backward compatible) plus
    # the account-executable Book B and the opportunity-loss analytics.
    return {
        "open_positions": len(open_trades),
        **_sliced(book_a),
        "book_b": _sliced(book_b),
        "opportunity_loss": _opportunity_loss(closed),
    }
