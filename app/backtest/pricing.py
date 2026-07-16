"""Backtest pricing — re-exports the shared quant pricing model.

Kept as a stable import location for the backtester; the single source of the
model is `app.quant.pricing`.
"""

from __future__ import annotations

from app.quant.pricing import (
    black_scholes_delta,
    black_scholes_price,
    net_position_price,
    plan_entry_net_per_share,
)

__all__ = [
    "black_scholes_delta",
    "black_scholes_price",
    "net_position_price",
    "plan_entry_net_per_share",
]
