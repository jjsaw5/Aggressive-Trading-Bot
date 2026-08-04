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

import re

from pydantic import BaseModel, Field

from app.analytics.metrics import (
    expectancy,
    max_drawdown,
    profit_factor,
    spearman,
    spearman_ci,
)
from app.domain.outcomes import DecisionOutcome, DecisionSnapshot, OutcomeResult
from app.logging_config import get_logger

log = get_logger(__name__)

# Short-duration decisions below this scoring-model version are DEGRADED: before v3,
# the scan never joined the IV-rank history, so the volatility factor fell back to a
# constant on every candidate (see config.scoring_model_version). They cannot be
# re-scored (the market snapshot is gone), so they are HARD-FILTERED out of any
# calibration corpus rather than trusted. Funnel-lineage decisions (empty or non-"sd-"
# version) are a different model and are not subject to this boundary.
_MIN_SD_CALIBRATION_VERSION = 3
# Matches the MAJOR version, tolerating a minor suffix. The previous pattern was
# `-v(\d+)$`, which stopped parsing anything the moment dotted versions arrived at
# v3.1 — so a hypothetical `sd-scoring-2025.01-v2.5` parsed as "not a short-duration
# version at all" and was admitted to the corpus as if undegraded. No such version
# exists today, so nothing was miscounted; the pattern is fixed here rather than
# left as a trap for the next minor release.
_SD_VERSION_RE = re.compile(r"^sd-scoring-.*-v(\d+)(?:\.\d+)*$")


def _is_degraded_short_duration(version: str) -> bool:
    m = _SD_VERSION_RE.match(version or "")
    return m is not None and int(m.group(1)) < _MIN_SD_CALIBRATION_VERSION


def eligible_for_calibration(
    snapshots: list[DecisionSnapshot],
) -> tuple[list[DecisionSnapshot], int]:
    """Drop degraded pre-v3 short-duration decisions from a calibration corpus and
    return (kept, n_excluded). Encoded, not documented: a caller cannot accidentally
    calibrate on contaminated scores. The exclusion count is logged."""
    kept, excluded = [], 0
    for s in snapshots:
        if _is_degraded_short_duration(s.scoring_model_version):
            excluded += 1
        else:
            kept.append(s)
    if excluded:
        log.warning("calibration_excluded_pre_v3", n_excluded=excluded, n_kept=len(kept))
    return kept, excluded


def _observation_only_buckets() -> set[str]:
    """Bucket NAMES that are captured but never calibrated. Today: {"0dte"}."""
    from app.config import settings

    return {
        s.strip() for s in settings.capture_observation_only_buckets.split(",") if s.strip()
    }


def _drop_observation_only(
    snapshots: list[DecisionSnapshot],
) -> tuple[list[DecisionSnapshot], int]:
    """Remove decisions from observation-only buckets, and return (kept, n).

    Matches on the recorded `dte_bucket`, NOT on `dte_at_entry`. The 0DTE selector
    admits dte 0 or 1 and the 1-5DTE selector starts at 1, so a 1-DTE row cannot be
    attributed to a bucket by its integer — filtering on the integer would silently
    drop legitimate 1-5DTE decisions along with the 0DTE ones.

    Decisions with an empty `dte_bucket` (pre-Amendment-3, and funnel lineage) are
    KEPT: absent is not evidence of membership.
    """
    buckets = _observation_only_buckets()
    if not buckets:
        return snapshots, 0
    kept, excluded = [], 0
    for s in snapshots:
        if s.dte_bucket and s.dte_bucket in buckets:
            excluded += 1
        else:
            kept.append(s)
    if excluded:
        log.warning(
            "calibration_excluded_observation_only",
            n_excluded=excluded, n_kept=len(kept), buckets=sorted(buckets),
        )
    return kept, excluded


def gradeable_outcomes(
    outcomes: list[DecisionOutcome],
) -> tuple[list[DecisionOutcome], int]:
    """Drop outcomes whose OBSERVATION was too sparse to grade, and return
    (kept, n_excluded).

    Amendment 3. P7 attached `mark_coverage_pct`, `max_gap_minutes` and
    `grade_confidence` to every outcome, and then nothing consumed them — so a
    grade with a 52-minute blind spot pooled with a densely-observed one and the
    resulting win rate could not be attributed between the signal and the hole in
    the observation.

    An exit that triggered and reversed inside a gap is MISSED, not mispriced, so
    the error is DIRECTIONAL: trades look longer-held than they were and stop-outs
    are under-reported. That bias does not average out with sample size, which is
    why this excludes rather than down-weights.

    `grade_confidence == "high"` is required. Empty string is the pre-P7 default
    and is treated as gradeable — those grades predate the measurement and their
    quality is unknown-but-not-known-bad; excluding them would silently discard
    the entire pre-P7 corpus. `"low"` and `"unknown"` are both dropped:
    "we could not tell how well we observed this" is not evidence of good
    observation.
    """
    kept, excluded = [], 0
    for o in outcomes:
        if o.grade_confidence in ("low", "unknown"):
            excluded += 1
        else:
            kept.append(o)
    if excluded:
        log.warning(
            "calibration_excluded_low_confidence_grades",
            n_excluded=excluded, n_kept=len(kept),
        )
    return kept, excluded

# Outcome fidelity for de-duplication: a closed paper trade (realized fill) and a
# live option-mark P&L are real dollars; the underlying-vs-breakeven proxy is a
# directional read with no P&L. Higher wins when a decision has several outcomes.
_FIDELITY = {
    "live_close": 3,  # a real fill the human actually got — ground truth
    "paper_trade": 3,
    # Settlement at expiry is EXACT, not a proxy: a defined-risk structure has no
    # extrinsic value left, so the underlying's close determines the payoff with
    # nothing modelled. High fidelity as a MEASUREMENT — but it grades a
    # hold-to-expiry POLICY the app does not run (its exit plan takes profit and
    # stops out earlier), which is flagged separately in the scorecard warnings.
    "expiry_settlement": 3,
    # The exit policy the app actually runs, replayed over real per-day marks.
    # This is the outcome source that answers "would the strategy have made
    # money" — expiry settlement answers the POP question instead.
    "managed_policy": 3,
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
    n_excluded_pre_v3: int = 0  # degraded pre-v3 short-duration decisions hard-filtered
    # Outcomes dropped because the session was too sparsely observed to grade
    # (grade_confidence low/unknown). Amendment 3 — reported, never silent: a
    # corpus that shrank is a fact about the data, not a detail.
    n_excluded_low_confidence: int = 0
    # Decisions from observation-only buckets (0DTE) — captured and paper-traded
    # for logic development, never calibrated. Amendment 3.
    n_excluded_observation_only: int = 0
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
    # Live vs scan vs paper: the human's real trades tracked as their own cohort.
    by_decision_source: list[GroupStat] = Field(default_factory=list)
    by_vol_regime: list[GroupStat] = Field(default_factory=list)
    by_horizon: list[GroupStat] = Field(default_factory=list)  # 0DTE / 1-5DTE / swing
    # POP calibration PER METHODOLOGY: a change in POP derivation (legacy funnel
    # analytics vs traded-expiry-IV) must never silently pool — each construct is
    # bucketed separately so "does 26% resolve ~26%?" is answered per source.
    pop_buckets_by_source: dict[str, list[Bucket]] = Field(default_factory=dict)
    # --- Shadow instrumentation: the sibling scanner's flow-quality metric ---
    # Recorded observationally (never fed into the score). These fields are the
    # ledger's verdict on whether it EARNS a place in scoring: does it separate
    # winners from losers on real-dollar outcomes, and does it beat the score we
    # already trust? Promotion is gated on a positive, non-degenerate answer here.
    by_flow_quality_band: list[GroupStat] = Field(default_factory=list)
    flow_quality_pnl_spearman: float | None = None  # corr(flow_quality, net P&L)
    score_pnl_spearman: float | None = None  # baseline: corr(composite_score, net P&L)
    # 95% bootstrap interval for the above, and the sample it rests on. A gate
    # cannot tell skill from noise without these.
    score_pnl_spearman_ci: list[float] | None = None
    score_pnl_n: int = 0
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


def select_pnl_outcomes(
    outcomes: list[DecisionOutcome],
) -> dict[str, DecisionOutcome]:
    """One outcome per decision for the DOLLAR metrics, preferring the grade of
    the policy the app actually runs.

    Win rate and Brier keep the hold-to-expiry grade because probability-of-profit
    is itself a hold-to-expiry claim. P&L is a different question — "would the
    strategy have made money" — and the strategy takes profit, stops out, and
    time-stops. So when a decision carries both an expiry settlement and a managed
    replay, the managed one wins here even though both are fidelity 3."""
    best: dict[str, DecisionOutcome] = {}
    for o in outcomes:
        if o.realized_pnl_usd is None:
            continue
        cur = best.get(o.decision_id)
        if cur is None:
            best[o.decision_id] = o
            continue
        cur_managed = cur.outcome_source == "managed_policy"
        new_managed = o.outcome_source == "managed_policy"
        if new_managed and not cur_managed:
            best[o.decision_id] = o
            continue
        if cur_managed and not new_managed:
            continue
        cf, nf = _fidelity(cur), _fidelity(o)
        if nf > cf or (nf == cf and (o.elapsed_days or 0) >= (cur.elapsed_days or 0)):
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
    # Hard boundary: degraded pre-v3 short-duration decisions never enter the corpus.
    snapshots, n_excluded_pre_v3 = eligible_for_calibration(snapshots)
    # Third hard boundary (Amendment 3): observation-only buckets are captured and
    # paper-traded, never calibrated. Filtered by BUCKET rather than only by
    # grade_confidence, because a 0DTE decision graded from DAILY marks carries the
    # pre-P7 empty confidence string and would otherwise pass the confidence filter
    # while being exactly the uninterpretable case the quarantine exists for.
    snapshots, n_excluded_observation_only = _drop_observation_only(snapshots)
    # Second hard boundary (Amendment 3): grades whose observation was too sparse
    # to trust never enter it either. Applied BEFORE outcome selection so a
    # low-confidence grade cannot win the fidelity tie-break and displace a
    # gradeable one.
    outcomes, n_excluded_low_confidence = gradeable_outcomes(outcomes)
    by_id = {s.decision_id: s for s in snapshots}
    chosen = select_scoring_outcomes(outcomes)
    pairs = [(by_id[i], o) for i, o in chosen.items() if i in by_id]

    decisive = [(s, o) for s, o in pairs if o.result in (OutcomeResult.WIN, OutcomeResult.LOSS)]
    wins = sum(1 for _, o in decisive if o.result == OutcomeResult.WIN)

    # Cost-adjusted P&L metrics from outcomes carrying a realized dollar P&L (real
    # marks or a closed paper trade), ordered by resolution time for drawdown.
    # Selected separately from `chosen` so the managed-exit replay — the policy the
    # app runs — supplies the dollars even when a hold-to-expiry grade also exists.
    priced = sorted(
        [(by_id[i], o) for i, o in select_pnl_outcomes(outcomes).items() if i in by_id],
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
    # Headline discrimination: does the composite score rank real P&L? Measured
    # over EVERY priced outcome — restricting it to flow-carrying decisions (as
    # the lift comparison must) left the gate unable to measure discrimination at
    # all whenever flow was absent.
    score_pnl_sp = spearman(
        [s.composite_score for s, _ in priced],
        [o.realized_pnl_usd for _, o in priced],
    )
    # A point estimate near zero is noise wearing a number. Carry the interval so
    # the gate can require the correlation to plausibly exclude zero rather than
    # merely be positive.
    score_pnl_ci = spearman_ci(
        [s.composite_score for s, _ in priced],
        [o.realized_pnl_usd for _, o in priced],
    )
    # Lift must compare like with like, so the baseline for it is re-measured on
    # the flow subsample only.
    score_sp_on_flow = spearman(
        [s.composite_score for s, _ in flow_priced],
        [o.realized_pnl_usd for _, o in flow_priced],
    )
    flow_lift = (
        round(flow_pnl_sp - score_sp_on_flow, 4)
        if (flow_pnl_sp is not None and score_sp_on_flow is not None)
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
    # Per-methodology POP calibration (empty pop_source = legacy funnel analytics).
    sources = sorted({s.pop_source or "funnel_analytics" for s, _ in pairs
                      if s.probability_of_profit is not None})
    pop_buckets_by_source = {
        src: [
            b for b in (
                _bucket(
                    f"{lo:.0%}-{min(hi, 1.0):.0%}",
                    [(s, o) for s, o in pairs
                     if s.probability_of_profit is not None
                     and (s.pop_source or "funnel_analytics") == src
                     and lo <= s.probability_of_profit < hi],
                )
                for lo, hi in _POP_EDGES
            ) if b.n > 0
        ]
        for src in sources
    }
    # LIVE decisions carry no engine score (composite_score is a placeholder), so
    # they are excluded from the score buckets — they are graded under their own
    # source group instead.
    from app.domain.outcomes import DecisionSource
    scored_pairs = [(s, o) for s, o in pairs if s.source != DecisionSource.LIVE]
    score_buckets = [
        _bucket(
            f"{lo:.2f}-{min(hi, 1.0):.2f}",
            [(s, o) for s, o in scored_pairs if lo <= s.composite_score < hi],
        )
        for lo, hi in _SCORE_EDGES
    ]

    warnings = _degeneracy_warnings(decisive)
    # Policy disclosure: expiry settlement measures the RIGHT thing for POP (a
    # hold-to-expiry probability) but the WRONG thing for "what would the managed
    # strategy have returned" — no profit target or stop is applied.
    n_expiry_pnl = sum(1 for _s, o in priced if o.outcome_source == "expiry_settlement")
    n_managed = sum(1 for _s, o in priced if o.outcome_source == "managed_policy")
    if priced and n_expiry_pnl >= len(priced) * 0.25:
        warnings.append(
            f"{n_expiry_pnl} of {len(priced)} priced outcome(s) are HOLD-TO-EXPIRY "
            "settlements. That is the correct grade for probability-of-profit (a "
            "hold-to-expiry claim) but NOT what the managed exit plan would have "
            "returned — targets and stops would have exited earlier. Read P&L-based "
            "discrimination here as policy-specific."
        )
    if n_managed:
        warnings.append(
            f"{n_managed} of {len(priced)} priced outcome(s) are MANAGED-POLICY "
            "replays: real daily marks walked forward under each decision's own "
            "target/stop/time-stop. Those dollars answer 'would the strategy have "
            "made money'; win rate and Brier still use the hold-to-expiry grade, "
            "which is what probability-of-profit actually claims."
        )
    if n_excluded_pre_v3:
        warnings.append(
            f"{n_excluded_pre_v3} pre-v3 short-duration decision(s) excluded as degraded "
            "(IV-rank bug); calibration is over eligible decisions only."
        )
    if n_excluded_observation_only:
        warnings.append(
            f"{n_excluded_observation_only} decision(s) excluded — from an "
            "OBSERVATION-ONLY bucket (0DTE), captured and paper-traded for logic "
            "development but never calibrated until its mark-quality bar is met."
        )
    if n_excluded_low_confidence:
        warnings.append(
            f"{n_excluded_low_confidence} grade(s) excluded — the session was too "
            "sparsely observed to grade (mark coverage / gap). An exit inside a gap "
            "is MISSED, not mispriced, so the bias is directional and does not "
            "average out with sample size."
        )

    return Scorecard(
        n_decisions=len(snapshots),
        n_resolved=len(pairs),
        n_decisive=len(decisive),
        n_excluded_pre_v3=n_excluded_pre_v3,
        n_excluded_low_confidence=n_excluded_low_confidence,
        n_excluded_observation_only=n_excluded_observation_only,
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
        pop_buckets_by_source=pop_buckets_by_source,
        score_buckets=[b for b in score_buckets if b.n > 0],
        by_strategy=_grouped(pairs, lambda s: s.strategy.display_name),
        by_direction=_grouped(pairs, lambda s: s.direction.value),
        by_decision_source=_grouped(pairs, lambda s: s.source.value),
        by_vol_regime=_grouped(regime_pairs, lambda s: _vol_regime(s.iv_rank)),
        by_horizon=_grouped(pairs, lambda s: _horizon(s.dte_at_entry)),
        by_flow_quality_band=_grouped(
            flow_band_pairs, lambda s: _flow_quality_band(s.flow_quality_proprietary)
        ),
        flow_quality_pnl_spearman=flow_pnl_sp,
        score_pnl_spearman=score_pnl_sp,
        score_pnl_spearman_ci=list(score_pnl_ci) if score_pnl_ci else None,
        score_pnl_n=len(priced),
        flow_quality_lift=flow_lift,
        flow_quality_verdict=flow_verdict,
        validation_grade=grade,
        warnings=warnings,
        note=(
            "P&L metrics are net of costs and come only from option-mark / "
            "paper-trade outcomes; the underlying-vs-breakeven proxy carries no "
            "P&L. validation_grade flags whether this rests on real marks. Win "
            "rate is over decisive outcomes (wins+losses); scratches excluded."
        ),
    )
