"""Aggregate backtest results into performance stats grouped by setup type.

Answers "how has this type of setup performed historically?" with the metrics
that matter for an aggressive-but-disciplined account: win rate, expectancy
(avg P&L per trade), profit factor, and the MFE/MAE distribution that reveals
whether exits are leaving money on the table or cutting too late.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.backtest.engine import BacktestResult


@dataclass
class PerformanceStats:
    group: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl_usd: float
    expectancy_usd: float  # avg P&L per trade
    profit_factor: float | None  # gross wins / gross losses (None if no losses)
    avg_mfe_usd: float
    avg_mae_usd: float
    avg_days_held: float

    def as_dict(self) -> dict:
        return {
            "group": self.group,
            "trades": self.trades,
            "win_rate": round(self.win_rate, 3),
            "expectancy_usd": round(self.expectancy_usd, 2),
            "total_pnl_usd": round(self.total_pnl_usd, 2),
            "profit_factor": round(self.profit_factor, 2) if self.profit_factor is not None else None,
            "avg_mfe_usd": round(self.avg_mfe_usd, 2),
            "avg_mae_usd": round(self.avg_mae_usd, 2),
            "avg_days_held": round(self.avg_days_held, 1),
        }


def _stats_for(group: str, results: list[BacktestResult]) -> PerformanceStats:
    n = len(results)
    pnls = [r.realized_pnl_usd for r in results]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    return PerformanceStats(
        group=group,
        trades=n,
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / n if n else 0.0,
        total_pnl_usd=sum(pnls),
        expectancy_usd=sum(pnls) / n if n else 0.0,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else None,
        avg_mfe_usd=sum(r.trade.mfe_usd for r in results) / n if n else 0.0,
        avg_mae_usd=sum(r.trade.mae_usd for r in results) / n if n else 0.0,
        avg_days_held=sum(r.days_held for r in results) / n if n else 0.0,
    )


def by_strategy(results: list[BacktestResult]) -> list[PerformanceStats]:
    return _grouped(results, lambda r: r.trade.trade_plan.strategy.display_name)


def by_direction(results: list[BacktestResult]) -> list[PerformanceStats]:
    return _grouped(results, lambda r: r.trade.trade_plan.direction.value)


def _grouped(
    results: list[BacktestResult], key: Callable[[BacktestResult], str]
) -> list[PerformanceStats]:
    buckets: dict[str, list[BacktestResult]] = {}
    for r in results:
        buckets.setdefault(key(r), []).append(r)
    stats = [_stats_for(g, rs) for g, rs in buckets.items()]
    stats.sort(key=lambda s: s.expectancy_usd, reverse=True)
    return stats


def overall(results: list[BacktestResult]) -> PerformanceStats:
    return _stats_for("ALL", results)
