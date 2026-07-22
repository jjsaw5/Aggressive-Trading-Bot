"""Turn warehoused decisions + outcomes into a self-scoring scorecard.

The point of the warehouse: grade our own suggestions. This module pairs each
decision with its ground-truth outcome and reports whether the engine's
predictions actually hold up:

- **Win rate** over decisive outcomes.
- **Direction accuracy** for directional theses.
- **POP calibration** — bucket by predicted probability of profit and compare to
  the realized win rate in each bucket. A well-calibrated engine's 70% bucket
  wins ~70% of the time.
- **Brier score** — mean squared error of the POP forecast (lower is better).
- **Score calibration** — is the composite score monotonic in realized win rate?

All pure functions over lists; no I/O.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.analytics.metrics import expectancy, max_drawdown, profit_factor, spearman
from app.domain.outcomes import DecisionOutcome, DecisionSnapshot, OutcomeResult

# Outcome fidelity for de-duplication: a closed paper trade (realized fill) and a
# live option-mark P&L are real dollars; the underlying-vs-breakeven proxy is a
# directional read with no P&L. Higher wins when a decision has several outcomes.
_FIDELITY = {
    "paper_trade": 3,
    "option_marks": 2,
    "option_marks_bs_fallback": 2,
    "underlying_vs_breakeven": 1,
}


def _fidelity(o: DecisionOutcome) -> int:
    return _FIDELITY.get(o.outcome_source, 0)


def _horizon(dte_at_entry: int | None) -> str:
    """Bucket a decision by its trade horizon so one ledger reports 0DTE, short-DTE
    and swing calibration side by side (Phase 4: merged horizons, one scorecard)."""
    if dte_at_entry is None:
        return "unknown"
    if dte_at_entry <= 1:
        return "0DTE"
    if dte_at_entry <= 5:
        return "1-5DTE"
    if dte_at_entry <= 55:
        return "swing"
    return "longer"


def _flow_quality_band(q: float | None) -> str | None:
    """Bucket the shadow flow-quality metric (see app/engine/flow_quality.py).
    Its floor is ~0.5 (a bare print), so bands are set to discriminate above it."""
    if q is None:
        return None
    if q < 0.6:
        return "weak"
    if q < 0.75:
        return "moderate"
    return "strong"


def _vol_regime(iv_rank: float | None) -> str | None:
    """Bucket IV rank into cheap/fair/rich/extreme (accepts 0-1 or 0-100)."""
    if iv_rank is None:
        return None
    r = iv_rank if iv_rank <= 1.0 else iv_rank / 100.0
    if r < 0.25:
        return "cheap"
    if r < 0.50:
        return "fair"
    if r <= 0.70:
        return "rich"
    return "extreme"


class Bucket(BaseModel):
    label: str
    n: int
    avg_predicted_pop: float | None = None
    realized_win_rate: float | None = None
    calibration_gap: float | None = None  # realized - predicted


class GroupStat(BaseModel):
    key: str
    n: int
    win_rate: float | None = None
    avg_score: float | None = None


class Scorecard(BaseModel):
    n_decisions: int
    n_resolved: int
    n_decisive: int  # wins + losses (excludes scratch/unknown)
    win_rate: float | None = None
    direction_accuracy: float | None = None
    avg_predicted_pop: float | None = None
    realized_win_rate: float | None = None
    calibration_gap: float | None = None
    brier_score: float | None = None
    # Cost-adjusted P&L metrics, from outcomes that carry a realized dollar P&L
    # (real option marks or a closed paper trade); the underlying proxy is excluded.
    net_pnl_usd: float | None = None
    expectancy_usd: float | None = None
    profit_factor: float | None = None
    max_drawdown_usd: float | None = None
    pop_buckets: list[Bucket] = Field(default_factory=list)
    score_buckets: list[Bucket] = Field(default_factory=list)
    by_strategy: list[GroupStat] = Field(default_factory=list)
    by_direction: list[GroupStat] = Field(default_factory=list)
    by_vol_regime: list[GroupStat] = Field(default_factory=list)
    by_horizon: list[GroupStat] = Field(default_factory=list)  # 0DTE / 1-5DTE / swing
    # --- Shadow instrumentation: the sibling scanner's flow-quality metric ---
    # Recorded observationally (never fed into the score). These fields are the
    # ledger's verdict on whether it EARNS a place in scoring: does it separate
    # winners from losers on real-dollar outcomes, and does it beat the score we
    # already trust? Promotion is gated on a positive, non-degenerate answer here.
    by_flow_quality_band: list[GroupStat] = Field(default_factory=list)
    flow_quality_pnl_spearman: float | None = None  # corr(flow_quality, net P&L)
    score_pnl_spearman: float | None = None  # baseline: corr(composite_score, net P&L)
    flow_quality_lift: float | None = None  # flow_quality corr minus score corr
    flow_quality_verdict: str = "insufficient"
    # Is this a trustworthy validation source? "real_marks" when most decisive
    # outcomes are option-mark / paper-trade P&L; "proxy_only" when it rests on the
    # directional underlying proxy; "insufficient" when nothing is decisive.
    validation_grade: str = "insufficient"
    warnings: list[str] = Field(default_factory=list)
    note: str = ""


_POP_EDGES = [(0.0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 1.0001)]
_SCORE_EDGES = [(0.0, 0.5), (0.5, 0.65), (0.65, 0.8), (0.8, 1.0001)]


def select_scoring_outcomes(
    outcomes: list[DecisionOutcome],
) -> dict[str, DecisionOutcome]:
    """One outcome per decision: prefer the realized paper-trade truth, else the
    longest-horizon underlying resolution."""
    best: dict[str, DecisionOutcome] = {}
    for o in outcomes:
        cur = best.get(o.decision_id)
        if cur is None:
            best[o.decision_id] = o
            continue
        cf, nf = _fidelity(cur), _fidelity(o)
        if nf > cf:
            best[o.decision_id] = o
        elif nf == cf and (o.elapsed_days or 0) >= (cur.elapsed_days or 0):
            best[o.decision_id] = o
    return best


def _rate(wins: int, decisive: int) -> float | None:
    return round(wins / decisive, 4) if decisive else None


def _bucket(
    label: str, pairs: list[tuple[DecisionSnapshot, DecisionOutcome]]
) -> Bucket:
    decisive = [(s, o) for s, o in pairs if o.result in (OutcomeResult.WIN, OutcomeResult.LOSS)]
    wins = sum(1 for _, o in decisive if o.result == OutcomeResult.WIN)
    pops = [s.probability_of_profit for s, _ in pairs if s.probability_of_profit is not None]
    avg_pop = round(sum(pops) / len(pops), 4) if pops else None
    win_rate = _rate(wins, len(decisive))
    gap = (
        round(win_rate - avg_pop, 4)
        if (win_rate is not None and avg_pop is not None)
        else None
    )
    return Bucket(
        label=label,
        n=len(pairs),
        avg_predicted_pop=avg_pop,
        realized_win_rate=win_rate,
        calibration_gap=gap,
    )


def _grouped(
    pairs: list[tuple[DecisionSnapshot, DecisionOutcome]],
    key_fn,
) -> list[GroupStat]:
    groups: dict[str, list[tuple[DecisionSnapshot, DecisionOutcome]]] = {}
    for s, o in pairs:
        groups.setdefault(key_fn(s), []).append((s, o))
    out: list[GroupStat] = []
    for key, items in sorted(groups.items()):
        decisive = [(s, o) for s, o in items if o.result in (OutcomeResult.WIN, OutcomeResult.LOSS)]
        wins = sum(1 for _, o in decisive if o.result == OutcomeResult.WIN)
        scores = [s.composite_score for s, _ in items]
        out.append(
            GroupStat(
                key=key,
                n=len(items),
                win_rate=_rate(wins, len(decisive)),
                avg_score=round(sum(scores) / len(scores), 4) if scores else None,
            )
        )
    return out


def _degeneracy_warnings(
    decisive: list[tuple[DecisionSnapshot, DecisionOutcome]]
) -> list[str]:
    """Flag a sample that cannot yet grade the score: too few losses (an unpaid
    short-vol tail reads as a win streak) or winners concentrated in one vol
    regime. Same guard the sibling scanner uses, so a lucky run never passes
    silently as validation."""
    warns: list[str] = []
    if not decisive:
        return warns
    losses = sum(1 for _, o in decisive if o.result == OutcomeResult.LOSS)
    if losses < 2:
        warns.append(
            f"only {losses} loss in {len(decisive)} decisive outcomes — the score "
            "cannot be calibrated against outcomes it has never seen (unpaid "
            "short-vol tail). Accumulate more resolved trades before trusting."
        )
    win_regimes = {_vol_regime(s.iv_rank) for s, o in decisive if o.result == OutcomeResult.WIN}
    win_regimes.discard(None)
    if len(win_regimes) == 1:
        warns.append(
            f"all winners sit in a single vol regime ({next(iter(win_regimes))}) — "
            "concentrated exposure, not diversified edge."
        )
    return warns


def _flow_quality_verdict(
    flow_priced: list[tuple[DecisionSnapshot, DecisionOutcome]],
    flow_pnl_sp: float | None,
    flow_lift: float | None,
) -> str:
    """Gate the shadow metric's promotion. Deliberately conservative — a thin or
    single-regime sample can manufacture a correlation, so it must clear a real
    sample size, show a positive P&L correlation, AND beat the incumbent score.
    Anything short of that reads as 'keep watching', never 'promote'."""
    n = len(flow_priced)
    if n < 10 or flow_pnl_sp is None:
        return "insufficient"
    losses = sum(1 for _, o in flow_priced if (o.realized_pnl_usd or 0) < 0)
    wins = sum(1 for _, o in flow_priced if (o.realized_pnl_usd or 0) > 0)
    if losses < 2 or wins < 2:
        return "insufficient"  # no spread to correlate against
    if flow_pnl_sp <= 0:
        return "not_predictive"
    if flow_lift is not None and flow_lift <= 0:
        return "no_lift_over_score"
    return "candidate_for_promotion"


def build_scorecard(
    snapshots: list[DecisionSnapshot], outcomes: list[DecisionOutcome]
) -> Scorecard:
    by_id = {s.decision_id: s for s in snapshots}
    chosen = select_scoring_outcomes(outcomes)
    pairs = [(by_id[i], o) for i, o in chosen.items() if i in by_id]

    decisive = [(s, o) for s, o in pairs if o.result in (OutcomeResult.WIN, OutcomeResult.LOSS)]
    wins = sum(1 for _, o in decisive if o.result == OutcomeResult.WIN)

    # Cost-adjusted P&L metrics from outcomes carrying a realized dollar P&L (real
    # marks or a closed paper trade), ordered by resolution time for drawdown.
    priced = sorted(
        [(s, o) for s, o in pairs if o.realized_pnl_usd is not None],
        key=lambda so: so[1].resolved_at,
    )
    pnls = [o.realized_pnl_usd for _, o in priced]
    real_n = sum(1 for _, o in decisive if _fidelity(o) >= 2)
    grade = (
        "real_marks" if decisive and real_n >= max(1, len(decisive) // 2)
        else "proxy_only" if decisive
        else "insufficient"
    )
    regime_pairs = [(s, o) for s, o in pairs if _vol_regime(s.iv_rank) is not None]

    # Shadow flow-quality: does the sibling's metric track real P&L, and does it
    # beat the composite score we already rely on? Measured only over outcomes
    # carrying a real dollar P&L, and only for decisions that had gradeable flow.
    flow_priced = [(s, o) for s, o in priced if s.flow_quality_proprietary is not None]
    flow_pnl_sp = spearman(
        [s.flow_quality_proprietary for s, _ in flow_priced],
        [o.realized_pnl_usd for _, o in flow_priced],
    )
    score_pnl_sp = spearman(
        [s.composite_score for s, _ in flow_priced],
        [o.realized_pnl_usd for _, o in flow_priced],
    )
    flow_lift = (
        round(flow_pnl_sp - score_pnl_sp, 4)
        if (flow_pnl_sp is not None and score_pnl_sp is not None)
        else None
    )
    flow_band_pairs = [(s, o) for s, o in pairs if s.flow_quality_proprietary is not None]
    flow_verdict = _flow_quality_verdict(flow_priced, flow_pnl_sp, flow_lift)

    # Direction accuracy over outcomes that carry a directional verdict.
    dir_calls = [o for _, o in pairs if o.direction_correct is not None]
    dir_correct = sum(1 for o in dir_calls if o.direction_correct)

    pops = [s.probability_of_profit for s, _ in pairs if s.probability_of_profit is not None]
    avg_pop = round(sum(pops) / len(pops), 4) if pops else None
    realized = _rate(wins, len(decisive))

    # Brier score over decisive outcomes that have a POP forecast.
    brier_terms = [
        (s.probability_of_profit, 1.0 if o.result == OutcomeResult.WIN else 0.0)
        for s, o in decisive
        if s.probability_of_profit is not None
    ]
    brier = (
        round(sum((p - y) ** 2 for p, y in brier_terms) / len(brier_terms), 4)
        if brier_terms
        else None
    )

    pop_buckets = [
        _bucket(
            f"{lo:.0%}-{min(hi, 1.0):.0%}",
            [
                (s, o)
                for s, o in pairs
                if s.probability_of_profit is not None and lo <= s.probability_of_profit < hi
            ],
        )
        for lo, hi in _POP_EDGES
    ]
    score_buckets = [
        _bucket(
            f"{lo:.2f}-{min(hi, 1.0):.2f}",
            [(s, o) for s, o in pairs if lo <= s.composite_score < hi],
        )
        for lo, hi in _SCORE_EDGES
    ]

    return Scorecard(
        n_decisions=len(snapshots),
        n_resolved=len(pairs),
        n_decisive=len(decisive),
        win_rate=realized,
        direction_accuracy=_rate(dir_correct, len(dir_calls)),
        avg_predicted_pop=avg_pop,
        realized_win_rate=realized,
        calibration_gap=(
            round(realized - avg_pop, 4)
            if (realized is not None and avg_pop is not None)
            else None
        ),
        brier_score=brier,
        net_pnl_usd=round(sum(pnls), 2) if pnls else None,
        expectancy_usd=expectancy(pnls),
        profit_factor=profit_factor(pnls),
        max_drawdown_usd=max_drawdown(pnls) if pnls else None,
        pop_buckets=[b for b in pop_buckets if b.n > 0],
        score_buckets=[b for b in score_buckets if b.n > 0],
        by_strategy=_grouped(pairs, lambda s: s.strategy.display_name),
        by_direction=_grouped(pairs, lambda s: s.direction.value),
        by_vol_regime=_grouped(regime_pairs, lambda s: _vol_regime(s.iv_rank)),
        by_horizon=_grouped(pairs, lambda s: _horizon(s.dte_at_entry)),
        by_flow_quality_band=_grouped(
            flow_band_pairs, lambda s: _flow_quality_band(s.flow_quality_proprietary)
        ),
        flow_quality_pnl_spearman=flow_pnl_sp,
        score_pnl_spearman=score_pnl_sp,
        flow_quality_lift=flow_lift,
        flow_quality_verdict=flow_verdict,
        validation_grade=grade,
        warnings=_degeneracy_warnings(decisive),
        note=(
            "P&L metrics are net of costs and come only from option-mark / "
            "paper-trade outcomes; the underlying-vs-breakeven proxy carries no "
            "P&L. validation_grade flags whether this rests on real marks. Win "
            "rate is over decisive outcomes (wins+losses); scratches excluded."
        ),
    )
