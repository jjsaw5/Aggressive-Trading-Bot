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
