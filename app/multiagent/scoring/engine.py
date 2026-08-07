"""The deterministic scoring engine.

Takes a validated candidate and returns a `CompositeScore` whose every point
traces to a measurement. No LLM output is an input to any arithmetic here — the
agents' contribution reached this point as *selection* (which ticker, which
catalyst, which direction), and the grading is entirely code.

Two properties worth stating because they are easy to lose:

* **Reproducible.** Same measurements plus same methodology version equals same
  score, every time. `tests/multiagent/test_scoring_engine.py` pins a golden
  case so a silent change to grading fails the suite.
* **Renormalised, not zero-filled.** Categories that could not be measured leave
  the denominator, and `input_coverage` reports how much of the 100 was live.
  A 78 at 55% coverage and a 78 at 100% coverage are different claims.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.multiagent.config import MethodologyConfig
from app.multiagent.models.candidates import ResearchCandidate
from app.multiagent.models.contracts import ProposedStructure
from app.multiagent.models.enums import CalibrationStatus, Classification
from app.multiagent.models.measurements import (
    AbsenceReason,
    Measurement,
    Provenance,
)
from app.multiagent.models.scoring import CompositeScore, ScoredCandidate
from app.multiagent.models.validation import ValidationReport
from app.multiagent.scoring.components import (
    score_catalyst_strength,
    score_contract_liquidity,
    score_data_quality,
    score_iv_greeks,
    score_market_alignment,
    score_options_flow,
    score_risk_reward,
    score_technical_setup,
)


def _quote_age_measurement(report: ValidationReport, now: datetime) -> Measurement:
    tech = report.technical
    if tech is None or tech.as_of is None:
        return Measurement.absent(
            "quote_age_seconds", AbsenceReason.NO_DATA, unit="s", note="no technical snapshot"
        )
    as_of = tech.as_of if tech.as_of.tzinfo else tech.as_of.replace(tzinfo=UTC)
    return Measurement.of(
        "quote_age_seconds",
        max(0.0, (now - as_of).total_seconds()),
        unit="s",
        provenance=Provenance.DERIVED,
        as_of=now,
        note="age of the underlying quote behind the technical snapshot",
    )


def score_candidate(
    candidate: ResearchCandidate,
    report: ValidationReport,
    cfg: MethodologyConfig,
    *,
    now: datetime | None = None,
    cross_check_disagreement_pct: float | None = None,
    structure: ProposedStructure | None = None,
) -> CompositeScore:
    """Grade one validated candidate. Deterministic given its inputs."""
    when = now or datetime.now(UTC)
    sc = cfg.scoring
    w = sc.weights
    chosen = structure or report.selected_structure()

    components = [
        score_catalyst_strength(report.catalyst, sc.catalyst_strength, w.catalyst_strength),
        score_market_alignment(report.alignment, sc.market_alignment, w.market_alignment),
        score_technical_setup(
            report.technical, candidate.direction, sc.technical_setup, w.technical_setup
        ),
        score_options_flow(report.flow, sc.options_flow, w.options_flow),
        score_iv_greeks(
            report.contract_quality,
            report.risk_reward,
            sc.iv_greeks,
            w.iv_greeks,
            long_delta_min=cfg.contracts.long_delta_min,
            long_delta_max=cfg.contracts.long_delta_max,
        ),
        score_contract_liquidity(report.contract_quality, sc.contract_liquidity, w.contract_liquidity),
        score_risk_reward(report.risk_reward, chosen, sc.risk_reward, w.risk_reward),
    ]

    # Data quality is scored last because its coverage rule reads how much of
    # the OTHER seven categories could be measured. Scoring it first would make
    # it grade a coverage figure that did not exist yet.
    measurable = sum(c.weight * (c.coverage or 0.0) for c in components)
    total_weight = sum(c.weight for c in components)
    coverage_value = (measurable / total_weight) if total_weight > 0 else None

    components.append(
        score_data_quality(
            cross_check=Measurement.of(
                "provider_price_disagreement_pct",
                cross_check_disagreement_pct,
                unit="%",
                provenance=Provenance.DERIVED,
                as_of=when,
                reason=AbsenceReason.NOT_IMPLEMENTED,
                note=(
                    "absolute percentage gap between independent underlying price sources; "
                    "absent when only one source answered"
                ),
            ),
            quote_age_seconds=_quote_age_measurement(report, when),
            coverage=Measurement.of(
                "scoring_input_coverage",
                round(coverage_value, 4) if coverage_value is not None else None,
                provenance=Provenance.DERIVED,
                as_of=when,
                note="share of the other seven categories' weight that had live inputs",
            ),
            cfg=sc.data_quality,
            weight=w.data_quality,
        )
    )

    return CompositeScore(
        candidate_id=candidate.candidate_id,
        run_id=candidate.run_id,
        methodology_version=cfg.version,
        scored_at=when,
        components=components,
        # docs/PRODUCT_STANCE.md: no feature has cleared out-of-sample
        # validation, so this is UNCALIBRATED and every surface says so.
        calibration_status=CalibrationStatus.UNCALIBRATED,
    )


def classify(score: CompositeScore, cfg: MethodologyConfig) -> tuple[Classification, str]:
    band = cfg.classification.classify(score.score)
    return Classification(band.label), band.name


def rank(
    scored: list[ScoredCandidate], cfg: MethodologyConfig
) -> list[ScoredCandidate]:
    """Order by score, then by coverage, then by ticker.

    Coverage breaks ties on purpose: between two candidates scoring 74, the one
    measured on more of its inputs is the better-understood trade. Ticker is the
    final key so ordering is stable and a re-run is diffable.
    """
    ordered = sorted(
        scored,
        key=lambda s: (-s.score.score, -s.score.input_coverage, s.ticker),
    )
    for i, s in enumerate(ordered, start=1):
        s.rank = i if s.is_ranked else None
    # Re-number only the ranked ones so display ranks are contiguous.
    n = 0
    for s in ordered:
        if s.is_ranked:
            n += 1
            s.rank = n
        else:
            s.rank = None
    return ordered
