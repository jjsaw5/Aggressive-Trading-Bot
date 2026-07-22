"""Cross-check the forward ledger against the real-mark backtest, trade by trade.

The forward book recorded each trade's outcome its own way (option marks or a
proxy). This reprices the SAME trades from recorded NBBO and compares. The point
is not a clean summary — it's the disagreements: a trade the ledger booked as a
win that real marks say was a loss (a sign flip) is exactly the "unpaid tail made
visible" case, and far more informative than an aggregate.

Pure over already-fetched bars, so the comparison is unit-tested without any DB
or API; a thin Turso/UW driver (scripts/forward_vs_backtest.py) feeds it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.backtest.fill_model import round_trip_pnl
from app.domain.historic import HistoricOptionBar


@dataclass(frozen=True)
class LedgerTrade:
    """One resolved forward-ledger trade, flattened to what a reprice needs."""

    ref: str
    symbol: str
    strategy: str
    entry_date: date
    resolved_date: date
    long_id: str | None
    short_id: str | None
    contracts: int
    recorded_net_pnl: float | None
    recorded_outcome: str | None  # "win" | "loss" | ... as the ledger stored it


def _result(pnl: float | None, *, scratch: float = 1.0) -> str | None:
    if pnl is None:
        return None
    if pnl > scratch:
        return "win"
    if pnl < -scratch:
        return "loss"
    return "scratch"


def _aligned_on_or_before(
    long_bars: list[HistoricOptionBar],
    short_bars: list[HistoricOptionBar],
    d: date,
    *,
    after: date | None = None,
) -> tuple[HistoricOptionBar, HistoricOptionBar, date] | None:
    """Both legs priced on a COMMON trading day <= d (rolling back together over a
    quote gap), optionally strictly after `after`. Aligning the legs on one date is
    what keeps entry and exit from silently drifting to different days."""
    ldates = {b.date: b for b in long_bars if b.mid is not None}
    sdates = {b.date: b for b in short_bars if b.mid is not None}
    common = sorted(
        dt for dt in (ldates.keys() & sdates.keys())
        if dt <= d and (after is None or dt > after)
    )
    if not common:
        return None
    dd = common[-1]
    return ldates[dd], sdates[dd], dd


@dataclass(frozen=True)
class CrossCheckRow:
    ref: str
    symbol: str
    strategy: str
    recorded_pnl: float | None
    real_mark_pnl_k1: float | None  # conservative (pays the spread)
    real_mark_pnl_k05: float | None  # optimistic
    recorded_result: str | None
    real_mark_result: str | None  # at k=1.0 (conservative)
    real_mark_result_opt: str | None  # at k=0.5 (optimistic / mid fills)
    repriced: bool
    agree: bool | None  # results match at k=1.0
    sign_flip: bool  # recorded win vs real-mark loss at k=1.0 (or vice versa)
    sign_flip_optimistic: bool  # flips even at k=0.5 — can't be blamed on the spread
    pnl_gap_usd: float | None  # recorded - real_mark(k=1)
    note: str = ""


def cross_check_trade(
    t: LedgerTrade,
    long_bars: list[HistoricOptionBar],
    short_bars: list[HistoricOptionBar],
) -> CrossCheckRow:
    recorded_result = t.recorded_outcome or _result(t.recorded_net_pnl)

    def _unrepriced(note: str) -> CrossCheckRow:
        return CrossCheckRow(
            ref=t.ref, symbol=t.symbol, strategy=t.strategy,
            recorded_pnl=t.recorded_net_pnl, real_mark_pnl_k1=None, real_mark_pnl_k05=None,
            recorded_result=recorded_result, real_mark_result=None, real_mark_result_opt=None,
            repriced=False, agree=None, sign_flip=False, sign_flip_optimistic=False,
            pnl_gap_usd=None, note=note,
        )

    if not t.long_id or not t.short_id:
        return _unrepriced("not a two-leg vertical / missing legs")
    entry = _aligned_on_or_before(long_bars, short_bars, t.entry_date)
    if entry is None:
        return _unrepriced("no aligned real marks on/before the entry date")
    el, es, entry_day = entry
    exit_ = _aligned_on_or_before(long_bars, short_bars, t.resolved_date, after=entry_day)
    if exit_ is None:
        return _unrepriced("no aligned real marks between entry and resolution date")
    xl, xs, _ = exit_

    k1 = round_trip_pnl(el, es, xl, xs, k=1.0, contracts=t.contracts)
    k05 = round_trip_pnl(el, es, xl, xs, k=0.5, contracts=t.contracts)
    if not k1.fillable or not k05.fillable:
        return _unrepriced("legs not fillable on entry/exit day")

    rm_result = _result(k1.net_pnl_usd)  # conservative
    rm_result_opt = _result(k05.net_pnl_usd)  # optimistic (mid fills)
    agree = recorded_result is not None and rm_result == recorded_result
    gap = (round(t.recorded_net_pnl - k1.net_pnl_usd, 2)
           if t.recorded_net_pnl is not None else None)
    return CrossCheckRow(
        ref=t.ref, symbol=t.symbol, strategy=t.strategy, recorded_pnl=t.recorded_net_pnl,
        real_mark_pnl_k1=round(k1.net_pnl_usd, 2), real_mark_pnl_k05=round(k05.net_pnl_usd, 2),
        recorded_result=recorded_result, real_mark_result=rm_result, real_mark_result_opt=rm_result_opt,
        repriced=True, agree=agree,
        sign_flip={recorded_result, rm_result} == {"win", "loss"},
        sign_flip_optimistic={recorded_result, rm_result_opt} == {"win", "loss"},
        pnl_gap_usd=gap,
    )


@dataclass
class CrossCheckSummary:
    n: int
    n_repriced: int
    n_agree: int
    n_sign_flip: int  # flips at k=1.0 (conservative — pays the spread)
    n_sign_flip_optimistic: int  # flips even at k=0.5 (mid) — not a slippage artifact
    flips: list[str]
    unrepriced_reasons: dict[str, int]


def summarize(rows: list[CrossCheckRow]) -> CrossCheckSummary:
    repriced = [r for r in rows if r.repriced]
    flips = [f"{r.ref} ({r.symbol} {r.strategy}): ledger {r.recorded_result} ${r.recorded_pnl} "
             f"vs real-mark {r.real_mark_result} ${r.real_mark_pnl_k1} (k=1.0)"
             + ("  [flips even at mid]" if r.sign_flip_optimistic else "")
             for r in repriced if r.sign_flip]
    reasons: dict[str, int] = {}
    for r in rows:
        if not r.repriced:
            reasons[r.note] = reasons.get(r.note, 0) + 1
    return CrossCheckSummary(
        n=len(rows),
        n_repriced=len(repriced),
        n_agree=sum(1 for r in repriced if r.agree),
        n_sign_flip=sum(1 for r in repriced if r.sign_flip),
        n_sign_flip_optimistic=sum(1 for r in repriced if r.sign_flip_optimistic),
        flips=flips,
        unrepriced_reasons=reasons,
    )
