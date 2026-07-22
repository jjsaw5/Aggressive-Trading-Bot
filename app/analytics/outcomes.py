"""Resolve a decision's realized outcome (ground truth).

Two resolvers, in order of fidelity:

1. `resolve_from_paper_trade` — when a simulated position actually closed, the
   realized P&L is the truth. Most accurate; used whenever a paper trade exists.

2. `resolve_underlying` — otherwise, score the decision against where the
   underlying finished versus the structure's breakeven(s). This is a *proxy*
   for intrinsic value at the horizon (no historical option feed is wired), and
   is labeled as such. It is exactly right at expiry and a reasonable directional
   read before then; it is the honest best we can do from underlying prices
   alone, and it is what POP is defined against (finishing past breakeven).
"""

from __future__ import annotations

from datetime import datetime

from app.domain.enums import Direction, StrategyType
from app.domain.outcomes import DecisionOutcome, DecisionSnapshot, OutcomeResult
from app.domain.trades import PaperTrade

# Structures whose profit region is "underlying beyond a single breakeven".
_BULLISH_SINGLE = {
    StrategyType.LONG_CALL,
    StrategyType.BULL_CALL_SPREAD,
    StrategyType.BULL_PUT_SPREAD,
}
_BEARISH_SINGLE = {
    StrategyType.LONG_PUT,
    StrategyType.BEAR_PUT_SPREAD,
    StrategyType.BEAR_CALL_SPREAD,
}
# Two-breakeven structures: long vol wins OUTSIDE the wings, short vol INSIDE.
_LONG_VOL = {StrategyType.LONG_STRADDLE, StrategyType.LONG_STRANGLE}
_SHORT_VOL = {StrategyType.IRON_CONDOR}


def _result_from_breakevens(
    strategy: StrategyType,
    breakevens: list[float],
    spot: float,
    band: float,
) -> OutcomeResult:
    bes = sorted(breakevens)
    if not bes:
        return OutcomeResult.UNKNOWN

    # Scratch if we finished within `band` (relative) of the nearest breakeven.
    nearest = min(bes, key=lambda b: abs(spot - b))
    if nearest > 0 and abs(spot - nearest) / nearest <= band:
        return OutcomeResult.SCRATCH

    if strategy in _BULLISH_SINGLE:
        return OutcomeResult.WIN if spot >= bes[0] else OutcomeResult.LOSS
    if strategy in _BEARISH_SINGLE:
        return OutcomeResult.WIN if spot <= bes[0] else OutcomeResult.LOSS
    if strategy in _LONG_VOL:
        return OutcomeResult.WIN if (spot <= bes[0] or spot >= bes[-1]) else OutcomeResult.LOSS
    if strategy in _SHORT_VOL:
        return OutcomeResult.WIN if bes[0] <= spot <= bes[-1] else OutcomeResult.LOSS
    return OutcomeResult.UNKNOWN


def _direction_correct(direction: Direction, ret_pct: float) -> bool | None:
    if direction == Direction.BULLISH:
        return ret_pct > 0
    if direction == Direction.BEARISH:
        return ret_pct < 0
    return None  # neutral / vol structures: no single-signed directional call


def resolve_underlying(
    snapshot: DecisionSnapshot,
    *,
    spot_now: float,
    resolved_at: datetime,
    horizon_label: str | None = None,
    scratch_band: float = 0.0025,
) -> DecisionOutcome:
    """Score a decision against the current underlying price."""
    entry = snapshot.entry_spot
    ret_pct = round((spot_now - entry) / entry * 100.0, 4) if entry else None
    elapsed = (resolved_at.date() - snapshot.generated_at.date()).days
    result = _result_from_breakevens(
        snapshot.strategy, snapshot.breakevens, spot_now, scratch_band
    )
    dir_ok = _direction_correct(snapshot.direction, ret_pct) if ret_pct is not None else None

    return DecisionOutcome(
        decision_id=snapshot.decision_id,
        symbol=snapshot.symbol,
        horizon_label=horizon_label or f"{elapsed}d",
        resolved_at=resolved_at,
        elapsed_days=elapsed,
        spot_at_resolution=round(spot_now, 4),
        underlying_return_pct=ret_pct,
        direction_correct=dir_ok,
        result=result,
        outcome_source="underlying_vs_breakeven",
        note=(
            "Intrinsic-at-horizon proxy from underlying vs breakeven; "
            "no option marks used."
        ),
    )


def _resolution_costs(n_legs: int, contracts: int, total_spread: float, s) -> float:
    """Round-trip cost of the structure at resolution: commission per leg per
    contract on BOTH open and close, plus slippage crossing the summed bid/ask
    spread (with a per-share floor). This is what turns a gross mark into a NET
    P&L the ledger can trust."""
    commission = contracts * n_legs * s.commission_per_contract_usd * 2
    slip_per_share = max(
        s.resolution_min_slippage_per_share,
        total_spread * s.resolution_slippage_spread_fraction,
    )
    slippage = contracts * 100.0 * slip_per_share
    return round(commission + slippage, 2)


def resolve_from_marks(
    snapshot: DecisionSnapshot,
    chain,
    *,
    resolved_at: datetime,
    horizon_label: str | None = None,
    scratch_band_frac: float = 0.05,
) -> DecisionOutcome | None:
    """Score a decision against LIVE OPTION MARKS — real intrinsic + extrinsic value,
    net of round-trip costs. This is the honest middle rung between a closed paper
    trade and the underlying-vs-breakeven proxy: the structure is marked leg-by-leg
    to the current chain (NBBO mid, Black-Scholes only as a labeled fallback), and
    P&L is the signed net change from the frozen entry net. Returns None when the
    chain cannot price the structure at all (caller falls back to the proxy)."""
    from app.config import settings
    from app.tiers.tier4_positions import mark_structure, position_iv

    plan = snapshot.trade_plan
    fallback_iv = position_iv(plan, chain) or snapshot.entry_iv
    mark = mark_structure(plan, chain, fallback_iv=fallback_iv, as_of=resolved_at.date())
    if mark is None:
        return None

    entry = snapshot.entry_net_per_share
    gross = round((mark.net - entry) * 100.0 * snapshot.contracts, 2)
    costs = _resolution_costs(len(plan.legs), snapshot.contracts, mark.total_spread, settings)
    net = round(gross - costs, 2)

    band = abs(snapshot.max_loss_usd) * scratch_band_frac if snapshot.max_loss_usd else 0.0
    result = (
        OutcomeResult.WIN if net > band
        else OutcomeResult.LOSS if net < -band
        else OutcomeResult.SCRATCH
    )
    elapsed = (resolved_at.date() - snapshot.generated_at.date()).days
    spot_now = getattr(chain, "underlying_price", None)
    ret_pct = (
        round((spot_now - snapshot.entry_spot) / snapshot.entry_spot * 100.0, 4)
        if spot_now and snapshot.entry_spot else None
    )
    marked = "; some legs Black-Scholes" if mark.used_bs else ""
    return DecisionOutcome(
        decision_id=snapshot.decision_id,
        symbol=snapshot.symbol,
        horizon_label=horizon_label or f"{elapsed}d",
        resolved_at=resolved_at,
        elapsed_days=elapsed,
        spot_at_resolution=round(spot_now, 4) if spot_now else None,
        underlying_return_pct=ret_pct,
        direction_correct=_direction_correct(snapshot.direction, ret_pct) if ret_pct is not None else None,
        result=result,
        realized_pnl_usd=net,
        realized_pnl_gross_usd=gross,
        costs_usd=costs,
        used_bs_fallback=mark.used_bs,
        outcome_source="option_marks_bs_fallback" if mark.used_bs else "option_marks",
        note=f"Marked to live chain (NBBO mid{marked}); net of ${costs:.0f} round-trip costs.",
    )


def resolve_from_paper_trade(
    snapshot: DecisionSnapshot, trade: PaperTrade
) -> DecisionOutcome | None:
    """Score a decision from a *closed* paper trade's realized P&L (best truth)."""
    if trade.realized_pnl_usd is None or trade.closed_at is None:
        return None
    pnl = trade.realized_pnl_usd
    if pnl > 0:
        result = OutcomeResult.WIN
    elif pnl < 0:
        result = OutcomeResult.LOSS
    else:
        result = OutcomeResult.SCRATCH
    elapsed = (trade.closed_at.date() - snapshot.generated_at.date()).days
    return DecisionOutcome(
        decision_id=snapshot.decision_id,
        symbol=snapshot.symbol,
        horizon_label="trade_close",
        resolved_at=trade.closed_at,
        elapsed_days=elapsed,
        result=result,
        realized_pnl_usd=pnl,
        outcome_source="paper_trade",
        note=f"Closed via {trade.exit_reason.value if trade.exit_reason else 'unknown'}.",
    )
