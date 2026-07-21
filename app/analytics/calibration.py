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

from app.domain.outcomes import DecisionOutcome, DecisionSnapshot, OutcomeResult


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
    pop_buckets: list[Bucket] = Field(default_factory=list)
    score_buckets: list[Bucket] = Field(default_factory=list)
    by_strategy: list[GroupStat] = Field(default_factory=list)
    by_direction: list[GroupStat] = Field(default_factory=list)
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
        cur_paper = cur.outcome_source == "paper_trade"
        new_paper = o.outcome_source == "paper_trade"
        if new_paper and not cur_paper:
            best[o.decision_id] = o
        elif new_paper == cur_paper and (o.elapsed_days or 0) >= (cur.elapsed_days or 0):
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


def build_scorecard(
    snapshots: list[DecisionSnapshot], outcomes: list[DecisionOutcome]
) -> Scorecard:
    by_id = {s.decision_id: s for s in snapshots}
    chosen = select_scoring_outcomes(outcomes)
    pairs = [(by_id[i], o) for i, o in chosen.items() if i in by_id]

    decisive = [(s, o) for s, o in pairs if o.result in (OutcomeResult.WIN, OutcomeResult.LOSS)]
    wins = sum(1 for _, o in decisive if o.result == OutcomeResult.WIN)

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
        pop_buckets=[b for b in pop_buckets if b.n > 0],
        score_buckets=[b for b in score_buckets if b.n > 0],
        by_strategy=_grouped(pairs, lambda s: s.strategy.display_name),
        by_direction=_grouped(pairs, lambda s: s.direction.value),
        note=(
            "Win rate is over decisive outcomes (wins+losses); scratches/unknowns "
            "excluded. Underlying-vs-breakeven outcomes are an intrinsic-at-horizon "
            "proxy; paper-trade outcomes use realized P&L."
        ),
    )
