"""Shared performance primitives — one implementation for the backtest and the
forward-outcome scorecard, so "profit factor" and "drawdown" mean the same thing
everywhere they are reported.
"""

from __future__ import annotations


def max_drawdown(pnls_in_order: list[float]) -> float:
    """Largest peak-to-trough drop of the cumulative P&L curve, as a non-negative
    USD number. Inputs MUST be in chronological (resolution/close) order — a
    drawdown computed on unordered P&L is meaningless. Empty -> 0.0."""
    cum = peak = mdd = 0.0
    for p in pnls_in_order:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return round(mdd, 2)


def profit_factor(pnls: list[float]) -> float | None:
    """Gross wins / gross losses. None when there are no losses (undefined, not
    'infinite edge' — a no-loss sample cannot be graded)."""
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    return round(gross_win / gross_loss, 3) if gross_loss > 0 else None


def expectancy(pnls: list[float]) -> float | None:
    """Average P&L per trade (USD). None on an empty sample."""
    return round(sum(pnls) / len(pnls), 2) if pnls else None
