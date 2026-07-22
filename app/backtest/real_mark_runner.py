"""Aggregate real-mark trade evaluations into a validating backtest report.

This is the §9 seam made runnable: given a set of verticals (each = two real
option contracts + an entry date + an exit rule), it fetches both legs' recorded
histories once (shared across trades), evaluates each with the causal real-mark
evaluator, and rolls the results up into cost-net, dual-k metrics — bucketed by
horizon and structure, with the same degeneracy guard the forward scorecard uses.

Unlike the Black-Scholes backtest, this one is a real validation source when it
rests on real marks: `validating=True` only for horizons the EOD feed can honestly
support (swing / short-DTE; never 0DTE).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.analytics.metrics import expectancy, max_drawdown, profit_factor
from app.backtest.real_mark import (
    ExitRule,
    RealMarkResult,
    RealMarkTrade,
    evaluate_real_mark_trade,
)
from app.logging_config import get_logger

log = get_logger(__name__)

_K_CONSERVATIVE = 1.0


@dataclass
class KStats:
    k: float
    n: int
    wins: int
    losses: int
    win_rate: float | None
    net_pnl_usd: float
    expectancy_usd: float | None
    profit_factor: float | None
    max_drawdown_usd: float
    avg_win_usd: float | None
    avg_loss_usd: float | None

    def as_dict(self) -> dict:
        return {
            "k": self.k, "n": self.n, "wins": self.wins, "losses": self.losses,
            "win_rate": self.win_rate, "net_pnl_usd": round(self.net_pnl_usd, 2),
            "expectancy_usd": self.expectancy_usd, "profit_factor": self.profit_factor,
            "max_drawdown_usd": round(self.max_drawdown_usd, 2),
            "avg_win_usd": self.avg_win_usd, "avg_loss_usd": self.avg_loss_usd,
        }


@dataclass
class GroupPnl:
    key: str
    n: int
    net_pnl_usd: float
    win_rate: float | None


@dataclass
class RealMarkReport:
    n_trades: int
    n_included: int
    n_excluded: int
    exclusions: dict[str, int]
    by_k: dict[float, KStats]
    by_horizon: list[GroupPnl]
    by_structure: list[GroupPnl]
    by_vol_regime: list[GroupPnl]
    slippage_fragile_n: int
    warnings: list[str] = field(default_factory=list)
    validating: bool = True
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "validating": self.validating,
            "n_trades": self.n_trades,
            "n_included": self.n_included,
            "n_excluded": self.n_excluded,
            "exclusions": self.exclusions,
            "by_k": {str(k): v.as_dict() for k, v in self.by_k.items()},
            "by_horizon": [(g.key, g.n, round(g.net_pnl_usd, 2), g.win_rate) for g in self.by_horizon],
            "by_structure": [(g.key, g.n, round(g.net_pnl_usd, 2), g.win_rate) for g in self.by_structure],
            "by_vol_regime": [(g.key, g.n, round(g.net_pnl_usd, 2), g.win_rate) for g in self.by_vol_regime],
            "slippage_fragile_n": self.slippage_fragile_n,
            "warnings": self.warnings,
            "note": self.note,
        }


def _net_pnls_at_k(
    pairs: list[tuple[RealMarkTrade, RealMarkResult]], k: float
) -> list[tuple[RealMarkTrade, RealMarkResult, float]]:
    """(trade, result, net_pnl) for fillable, included trades at fraction k,
    ordered by exit date so the drawdown curve is chronological."""
    out = []
    for t, r in pairs:
        if not r.included:
            continue
        rt = r.by_k.get(k)
        if rt is None or not rt.fillable or rt.net_pnl_usd is None:
            continue
        out.append((t, r, rt.net_pnl_usd))
    out.sort(key=lambda x: x[1].exit_date or x[1].entry_date)
    return out


def _kstats(pairs: list[tuple[RealMarkTrade, RealMarkResult]], k: float) -> KStats:
    priced = _net_pnls_at_k(pairs, k)
    pnls = [p for _, _, p in priced]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    return KStats(
        k=k, n=len(pnls), wins=len(wins), losses=len(losses),
        win_rate=round(len(wins) / len(pnls), 4) if pnls else None,
        net_pnl_usd=sum(pnls),
        expectancy_usd=expectancy(pnls),
        profit_factor=profit_factor(pnls),
        max_drawdown_usd=max_drawdown(pnls),
        avg_win_usd=round(sum(wins) / len(wins), 2) if wins else None,
        avg_loss_usd=round(sum(losses) / len(losses), 2) if losses else None,
    )


def _grouped(
    priced: list[tuple[RealMarkTrade, RealMarkResult, float]], key_fn
) -> list[GroupPnl]:
    groups: dict[str, list[float]] = {}
    for t, r, pnl in priced:
        groups.setdefault(key_fn(t, r), []).append(pnl)
    out = []
    for key, pnls in sorted(groups.items()):
        wins = sum(1 for p in pnls if p > 0)
        out.append(GroupPnl(key=key, n=len(pnls), net_pnl_usd=sum(pnls),
                            win_rate=round(wins / len(pnls), 4) if pnls else None))
    return out


def _degeneracy_warnings(
    priced: list[tuple[RealMarkTrade, RealMarkResult, float]]
) -> list[str]:
    """Same guard as the forward scorecard: too few losses, or winners in a single
    vol regime, means the sample can't yet grade the structure — a lucky run must
    not read as validation."""
    warns: list[str] = []
    if not priced:
        return ["no fillable trades — nothing to validate."]
    losses = sum(1 for _, _, p in priced if p < 0)
    if losses < 2:
        warns.append(
            f"only {losses} loss in {len(priced)} trades — cannot grade a structure "
            "against outcomes it has barely seen (unpaid-tail risk)."
        )
    win_regimes = {t.vol_regime for t, _, p in priced if p > 0 and t.vol_regime}
    win_regimes.discard(None)
    win_regimes.discard("unknown")
    if len(win_regimes) == 1:
        warns.append(
            f"all winners sit in a single vol regime ({next(iter(win_regimes))}) — "
            "concentrated exposure, not diversified edge."
        )
    return warns


def aggregate(
    pairs: list[tuple[RealMarkTrade, RealMarkResult]],
    *,
    ks: tuple[float, ...] = (0.5, 1.0),
) -> RealMarkReport:
    included = [(t, r) for t, r in pairs if r.included]
    excluded = [(t, r) for t, r in pairs if not r.included]
    exclusions: dict[str, int] = {}
    for _, r in excluded:
        exclusions[r.exclusion_reason or "unknown"] = exclusions.get(r.exclusion_reason or "unknown", 0) + 1

    priced_con = _net_pnls_at_k(included, _K_CONSERVATIVE)
    return RealMarkReport(
        n_trades=len(pairs),
        n_included=len(included),
        n_excluded=len(excluded),
        exclusions=exclusions,
        by_k={k: _kstats(included, k) for k in ks},
        by_horizon=_grouped(priced_con, lambda t, r: r.horizon),
        by_structure=_grouped(priced_con, lambda t, r: t.strategy),
        by_vol_regime=_grouped(priced_con, lambda t, r: t.vol_regime or "unknown"),
        slippage_fragile_n=sum(1 for _, r in included if r.slippage_fragile),
        warnings=_degeneracy_warnings(priced_con),
        note=(
            "Real-mark, cost-net P&L from recorded UW NBBO. Metrics reported at "
            "k=0.5 (optimistic) and k=1.0 (pays the full spread); a strategy positive "
            "only at 0.5 is slippage-fragile. Grouped stats are at k=1.0 (conservative)."
        ),
    )


async def run_real_mark_backtest(
    trades: list[tuple[RealMarkTrade, ExitRule]],
    provider,
    *,
    window_days: int = 400,
) -> RealMarkReport:
    """Fetch each unique contract's history once, evaluate every trade against the
    shared cache, and aggregate. One bad contract becomes an exclusion, never a
    crash."""
    from datetime import timedelta

    ids = {t.long_id for t, _ in trades} | {t.short_id for t, _ in trades}
    cache: dict[str, list] = {}
    for cid in sorted(ids):
        try:
            cache[cid] = await provider.get_contract_history(cid)
        except Exception as exc:  # noqa: BLE001 — isolate, tag trades as excluded
            log.warning("real_mark_contract_fetch_failed", contract_id=cid, error=str(exc))
            cache[cid] = []

    pairs: list[tuple[RealMarkTrade, RealMarkResult]] = []
    for trade, rule in trades:
        # Slice to a causal-ish window around entry to keep evaluation cheap.
        lo = trade.entry_date - timedelta(days=window_days)
        hi = trade.entry_date + timedelta(days=window_days)
        long_bars = [b for b in cache.get(trade.long_id, []) if lo <= b.date <= hi]
        short_bars = [b for b in cache.get(trade.short_id, []) if lo <= b.date <= hi]
        pairs.append((trade, evaluate_real_mark_trade(trade, long_bars, short_bars, rule)))
    return aggregate(pairs)
