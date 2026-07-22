"""Flow-alpha experiment orchestration (spec §1/§5).

Pure over already-tagged trades: each trade carries its regime, direction, net P&L
at each k, and its flow features. This module tags arms with a threshold set,
computes the beta-neutralized CONFIRM−OPPOSE spread (within each regime×direction
cell, pooled over cells that hold both arms), runs the walk-forward threshold
search reporting the whole out-of-sample grid distribution (never the argmax), and
renders the verdict against the five pre-registered criteria.

Never resolves at mid — the trade's net P&L is supplied pre-resolved at real
bid/ask (k=1.0 for the verdict). This module only compares arms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.backtest.flow_proxy import FlowFeatures, FlowThresholds, flow_arm
from app.backtest.stats import DiffCI, bootstrap_diff_ci, summarize_grid
from app.backtest.walk_forward import walk_forward_folds

_K_VERDICT = 1.0
_MIN_LOSSES = 2  # loss floor per arm (degeneracy guard)


@dataclass(frozen=True)
class ExpTrade:
    entry_date: date
    exit_date: date
    direction: str  # "bullish" | "bearish"
    regime: str
    net_by_k: dict[float, float]  # k -> net P&L (real bid/ask)
    flow: FlowFeatures | None


def _cell(t: ExpTrade) -> tuple[str, str]:
    return (t.regime, t.direction)


def pooled_confirm_minus_oppose(
    trades: list[ExpTrade], thr: FlowThresholds, k: float
) -> DiffCI | None:
    """CONFIRM−OPPOSE net-P&L spread, pooled over (regime×direction) cells that hold
    BOTH arms — the beta control. None if no cell qualifies."""
    cells: dict[tuple[str, str], dict[str, list[float]]] = {}
    for t in trades:
        arm = flow_arm(t.flow, t.direction, thr)
        if arm == "NEUTRAL":
            continue
        cells.setdefault(_cell(t), {"CONFIRM": [], "OPPOSE": []})[arm].append(t.net_by_k[k])
    confirm: list[float] = []
    oppose: list[float] = []
    for arms in cells.values():
        if arms["CONFIRM"] and arms["OPPOSE"]:  # within-cell match only
            confirm += arms["CONFIRM"]
            oppose += arms["OPPOSE"]
    if not confirm or not oppose:
        return None
    return bootstrap_diff_ci(confirm, oppose)


def _arm_losses(trades: list[ExpTrade], thr: FlowThresholds, arm_name: str, k: float) -> int:
    return sum(
        1 for t in trades
        if flow_arm(t.flow, t.direction, thr) == arm_name and t.net_by_k[k] < 0
    )


@dataclass
class FoldResult:
    index: int
    best_thr: FlowThresholds | None
    test_spread: float | None  # OOS pooled CONFIRM−OPPOSE at the tuned θ (k=1.0)
    grid_oos_lifts: list[float] = field(default_factory=list)  # every θ's OOS spread


@dataclass
class ExperimentResult:
    n_trades: int
    n_folds: int
    fold_results: list[FoldResult]
    same_sign_all_folds: bool
    grid_summary: object  # GridSummary over all folds' OOS lifts
    pooled_ci: DiffCI | None  # full-sample pooled CONFIRM−OPPOSE at k=1.0 (best θ)
    loss_floor_ok: bool
    verdict: str
    reasons: list[str]


def _tune(train: list[ExpTrade], grid: list[FlowThresholds], k: float) -> FlowThresholds | None:
    best, best_pt = None, None
    for thr in grid:
        ci = pooled_confirm_minus_oppose(train, thr, k)
        if ci is not None and (best_pt is None or ci.point > best_pt):
            best, best_pt = thr, ci.point
    return best


def run_experiment(
    trades: list[ExpTrade],
    grid: list[FlowThresholds],
    *,
    n_folds: int,
    embargo_days: int,
    material_margin: float,
    reverse: bool = False,
) -> ExperimentResult:
    folds = walk_forward_folds(
        trades, entry_of=lambda t: t.entry_date, exit_of=lambda t: t.exit_date,
        n_folds=n_folds, embargo_days=embargo_days, reverse=reverse,
    )
    fold_results: list[FoldResult] = []
    all_oos: list[float] = []
    for f in folds:
        best = _tune(f.train, grid, _K_VERDICT)
        test_spread = None
        if best is not None:
            ci = pooled_confirm_minus_oppose(f.test, best, _K_VERDICT)
            test_spread = ci.point if ci else None
        grid_lifts = [ci.point for thr in grid
                      if (ci := pooled_confirm_minus_oppose(f.test, thr, _K_VERDICT)) is not None]
        all_oos += grid_lifts
        fold_results.append(FoldResult(f.index, best, test_spread, grid_lifts))

    test_spreads = [fr.test_spread for fr in fold_results if fr.test_spread is not None]
    same_sign = bool(test_spreads) and (
        all(s > 0 for s in test_spreads) or all(s < 0 for s in test_spreads)
    )
    # Full-sample estimate at the globally-best θ (reported alongside, not the verdict).
    best_global = _tune(trades, grid, _K_VERDICT)
    pooled_ci = pooled_confirm_minus_oppose(trades, best_global, _K_VERDICT) if best_global else None
    loss_floor_ok = best_global is not None and (
        _arm_losses(trades, best_global, "CONFIRM", _K_VERDICT) >= _MIN_LOSSES
        and _arm_losses(trades, best_global, "OPPOSE", _K_VERDICT) >= _MIN_LOSSES
    )

    reasons: list[str] = []
    if len(trades) < 200:
        reasons.append(f"insufficient sample ({len(trades)} trades) — below a differential-test floor")
    if not test_spreads:
        reasons.append("no walk-forward fold produced both arms in a matched cell")
    if not same_sign:
        reasons.append("CONFIRM−OPPOSE reversed sign across folds — regime-conditional at best")
    if pooled_ci is None or not pooled_ci.excludes_zero:
        reasons.append("bootstrap 95% CI does not exclude zero")
    if pooled_ci is not None and pooled_ci.point < material_margin:
        reasons.append(f"pooled spread ${pooled_ci.point:.2f} below material margin ${material_margin:.2f}")
    if not loss_floor_ok:
        reasons.append("an arm is below the real-loss floor (uncalibratable)")

    verdict = "fail_to_reject_H0" if reasons else "candidate_edge_proxy_only"
    return ExperimentResult(
        n_trades=len(trades), n_folds=len(folds), fold_results=fold_results,
        same_sign_all_folds=same_sign, grid_summary=summarize_grid(all_oos),
        pooled_ci=pooled_ci, loss_floor_ok=loss_floor_ok, verdict=verdict, reasons=reasons,
    )
