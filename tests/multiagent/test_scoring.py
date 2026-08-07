"""The deterministic scoring engine: weights, abstention, and auditability.

Three properties are load-bearing and each gets its own test:

1. **Every point traces to a measurement.** The composite reconstructs exactly
   from its leaves. If it does not, the audit trail is decoration.
2. **Absent is not zero.** A missing input removes its weight from the
   denominator instead of scoring zero, and the coverage figure says so.
3. **Category rule points sum to the configured weight.** Otherwise a category
   silently caps below its stated contribution.
"""

from __future__ import annotations

import pytest

from app.multiagent.config import CATEGORY_ORDER, TOTAL_POINTS
from app.multiagent.models.enums import CalibrationStatus, MeasurementStatus
from app.multiagent.models.measurements import AbsenceReason, Measurement, Provenance
from app.multiagent.models.scoring import CompositeScore, ScoreComponent, ScoreRule
from app.multiagent.scoring.rules import (
    abstain,
    band_rule,
    boolean_rule,
    penalty_rule,
    threshold_rule,
)


def _m(name: str, value: float | None) -> Measurement:
    return Measurement.of(name, value, provenance=Provenance.DERIVED)


# --- weights ---------------------------------------------------------------


def test_configured_weights_total_one_hundred(methodology):
    total = sum(methodology.scoring.weights.for_category(c) for c in CATEGORY_ORDER)
    assert total == TOTAL_POINTS


@pytest.mark.parametrize(
    ("category", "rule_keys"),
    [
        (
            "catalyst_strength",
            ["confirmed_scheduled", "sourced_news", "timing_within_horizon", "high_importance"],
        ),
        (
            "market_alignment",
            ["spy_aligned", "qqq_aligned", "sector_aligned", "relative_strength"],
        ),
        (
            "technical_setup",
            [
                "trend_aligned",
                "above_below_key_ma",
                "relative_volume",
                "momentum_confirmation",
                "room_to_target",
                "atr_supports_move",
            ],
        ),
        (
            "options_flow",
            [
                "directional_agreement",
                "ask_side_aggression",
                "sweep_presence",
                "size_vs_open_interest",
                "concentration",
            ],
        ),
        (
            "iv_greeks",
            ["iv_rank_favorable", "term_structure_ok", "delta_in_band", "theta_tolerable"],
        ),
        ("contract_liquidity", ["spread_tight", "open_interest", "volume"]),
        (
            "risk_reward",
            ["reward_to_risk", "breakeven_reachable", "within_risk_budget", "invalidation_defined"],
        ),
        ("data_quality", ["providers_agree", "data_fresh", "full_coverage"]),
    ],
)
def test_each_category_rule_points_sum_to_its_weight(methodology, category, rule_keys):
    """A category whose rules do not sum to its weight can never reach it."""
    rules = methodology.scoring.rules_for(category)
    weight = methodology.scoring.weights.for_category(category)
    total = sum(getattr(rules, key) for key in rule_keys)
    assert total == weight, (
        f"{category}: credit rules sum to {total} but the category weight is {weight}. "
        "A shortfall caps the category below its stated contribution; an excess lets it "
        "exceed the weight before clamping."
    )


def test_penalties_are_negative_so_they_can_only_subtract(methodology):
    for category in CATEGORY_ORDER:
        rules = methodology.scoring.rules_for(category)
        for key, value in rules.model_dump().items():
            if key.endswith("_penalty"):
                assert value < 0, f"{category}.{key} must be negative, got {value}"


# --- abstention ------------------------------------------------------------


def test_an_absent_input_abstains_rather_than_scoring_zero():
    absent = Measurement.absent("thing", AbsenceReason.NO_DATA)
    rule = threshold_rule("r", "d", absent, points=5.0, threshold=1.0)

    assert rule.status is MeasurementStatus.ABSTAINED
    assert rule.points_awarded == 0.0
    # The distinction: possible points are reported, but do NOT count.
    assert rule.points_possible == 5.0
    assert rule.counted_possible == 0.0


def test_a_measured_failure_scores_zero_and_keeps_its_denominator():
    rule = threshold_rule("r", "d", _m("thing", 0.1), points=5.0, threshold=1.0)
    assert rule.status is MeasurementStatus.MEASURED
    assert rule.points_awarded == 0.0
    assert rule.counted_possible == 5.0


def test_abstention_and_measured_failure_produce_different_scores():
    """The whole reason abstention exists."""
    measured_bad = ScoreComponent(
        category="c",
        weight=10.0,
        rules=[
            threshold_rule("a", "d", _m("x", 5.0), points=5.0, threshold=1.0),
            threshold_rule("b", "d", _m("y", 0.0), points=5.0, threshold=1.0),
        ],
    )
    could_not_measure = ScoreComponent(
        category="c",
        weight=10.0,
        rules=[
            threshold_rule("a", "d", _m("x", 5.0), points=5.0, threshold=1.0),
            threshold_rule(
                "b", "d", Measurement.absent("y", AbsenceReason.NO_DATA), points=5.0, threshold=1.0
            ),
        ],
    )
    assert measured_bad.points_awarded == 5.0
    assert measured_bad.points_available == 10.0
    assert measured_bad.normalized == 5.0
    assert measured_bad.coverage == 1.0

    assert could_not_measure.points_awarded == 5.0
    assert could_not_measure.points_available == 5.0
    # Same points earned, but extrapolated to full weight because half the
    # category was unmeasurable — and coverage says which is which.
    assert could_not_measure.normalized == 10.0
    assert could_not_measure.coverage == 0.5


def test_a_fully_unmeasurable_category_abstains_and_leaves_the_composite():
    dead = ScoreComponent(
        category="dead",
        weight=20.0,
        rules=[abstain("a", "d", 20.0, Measurement.absent("x", AbsenceReason.NO_DATA))],
    )
    live = ScoreComponent(
        category="live",
        weight=80.0,
        rules=[threshold_rule("b", "d", _m("y", 5.0), points=80.0, threshold=1.0)],
    )
    score = CompositeScore(
        candidate_id="c", run_id="r", methodology_version="v", scored_at=_ts(), components=[dead, live]
    )
    assert dead.abstained
    assert score.measured_weight == 80.0
    assert score.raw_points == 80.0
    # Renormalised: the dead category does not cap the score at 80.
    assert score.score == 100.0
    assert score.input_coverage == 0.8


def test_coverage_reflects_partial_measurement_inside_a_category():
    comp = ScoreComponent(
        category="c",
        weight=100.0,
        rules=[
            threshold_rule("a", "d", _m("x", 5.0), points=40.0, threshold=1.0),
            abstain("b", "d", 60.0, Measurement.absent("y", AbsenceReason.NO_DATA)),
        ],
    )
    score = CompositeScore(
        candidate_id="c", run_id="r", methodology_version="v", scored_at=_ts(), components=[comp]
    )
    assert score.input_coverage == 0.4
    assert score.score == 100.0  # everything measurable was earned


# --- penalties -------------------------------------------------------------


def test_a_penalty_subtracts_without_raising_the_denominator():
    comp = ScoreComponent(
        category="c",
        weight=10.0,
        rules=[
            threshold_rule("credit", "d", _m("x", 5.0), points=10.0, threshold=1.0),
            penalty_rule("pen", "d", True, points=-4.0, measurement=_m("y", 1.0)),
        ],
    )
    assert comp.points_available == 10.0  # penalty adds nothing to the denominator
    assert comp.points_awarded == 6.0


def test_a_category_cannot_go_negative_from_penalties():
    comp = ScoreComponent(
        category="c",
        weight=10.0,
        rules=[
            threshold_rule("credit", "d", _m("x", 0.0), points=10.0, threshold=1.0),
            penalty_rule("pen", "d", True, points=-99.0, measurement=_m("y", 1.0)),
        ],
    )
    # Clamped at zero: a runaway penalty must not steal points from other categories.
    assert comp.points_awarded == 0.0


def test_an_unmeasurable_penalty_does_not_fire():
    rule = penalty_rule("pen", "d", None, points=-5.0)
    assert rule.points_awarded == 0.0
    assert rule.counted_possible == 0.0
    assert "not applied" in rule.detail


# --- partial credit --------------------------------------------------------


def test_partial_credit_scales_from_the_floor_not_from_zero():
    """A measurement at its no-signal baseline earns nothing."""
    at_baseline = threshold_rule(
        "rv", "relative volume", _m("relative_volume", 1.0),
        points=3.0, threshold=1.3, partial=True, floor=1.0,
    )
    assert at_baseline.points_awarded == 0.0

    halfway = threshold_rule(
        "rv", "relative volume", _m("relative_volume", 1.15),
        points=3.0, threshold=1.3, partial=True, floor=1.0,
    )
    assert halfway.points_awarded == pytest.approx(1.5, abs=0.01)

    clears = threshold_rule(
        "rv", "relative volume", _m("relative_volume", 1.4),
        points=3.0, threshold=1.3, partial=True, floor=1.0,
    )
    assert clears.points_awarded == 3.0


def test_partial_credit_never_exceeds_the_points_or_goes_negative():
    below_floor = threshold_rule(
        "rv", "d", _m("x", 0.2), points=3.0, threshold=1.3, partial=True, floor=1.0
    )
    assert below_floor.points_awarded == 0.0


# --- tri-state booleans ----------------------------------------------------


def test_an_unanswerable_boolean_abstains_rather_than_counting_as_false():
    rule = boolean_rule("b", "d", None, points=4.0)
    assert rule.abstained
    assert rule.counted_possible == 0.0


def test_band_rule_requires_the_value_inside_the_band():
    assert band_rule("b", "d", _m("x", 0.5), points=2.0, low=0.35, high=0.65).points_awarded == 2.0
    assert band_rule("b", "d", _m("x", 0.1), points=2.0, low=0.35, high=0.65).points_awarded == 0.0
    assert band_rule(
        "b", "d", Measurement.absent("x", AbsenceReason.NO_DATA), points=2.0, low=0.0, high=1.0
    ).abstained


# --- auditability ----------------------------------------------------------


def test_the_composite_reconstructs_exactly_from_its_leaves():
    """The auditability requirement, mechanised."""
    comps = [
        ScoreComponent(
            category=f"c{i}",
            weight=20.0,
            rules=[
                threshold_rule("a", "d", _m("x", float(i)), points=12.0, threshold=1.0),
                threshold_rule("b", "d", _m("y", float(i)), points=8.0, threshold=3.0),
            ],
        )
        for i in range(1, 6)
    ]
    score = CompositeScore(
        candidate_id="c", run_id="r", methodology_version="v", scored_at=_ts(), components=comps
    )

    for comp in comps:
        leaf_awarded = sum(r.points_awarded for r in comp.rules if not r.abstained)
        leaf_possible = sum(r.counted_possible for r in comp.rules)
        assert comp.points_awarded == pytest.approx(leaf_awarded)
        assert comp.points_available == pytest.approx(leaf_possible)
        assert comp.normalized == pytest.approx(leaf_awarded / leaf_possible * comp.weight)

    assert score.raw_points == pytest.approx(
        sum(c.normalized for c in comps if c.normalized is not None)
    )
    assert score.score == pytest.approx(score.raw_points / score.measured_weight * 100.0)


def test_every_rule_explains_itself_with_its_measurement():
    rule = threshold_rule("liq.oi", "open interest", _m("min_open_interest", 1500.0), points=3.0, threshold=1000.0)
    line = rule.explain()
    assert "liq.oi" in line
    assert "3/3" in line
    assert "1500" in line
    assert "1000" in line


def test_an_abstained_rule_says_so_and_names_the_reason():
    rule = threshold_rule(
        "liq.oi", "open interest",
        Measurement.absent("min_open_interest", AbsenceReason.PROVIDER_ERROR),
        points=3.0, threshold=1000.0,
    )
    line = rule.explain()
    assert "ABSTAINED" in line
    assert "NA_provider_error" in line
    assert "removed from denominator" in line


def test_a_penalty_that_did_not_fire_reads_as_not_triggered():
    rule = penalty_rule("t.ext", "extended", False, points=-3.0, measurement=_m("extension_atr", 1.2), threshold=2.5)
    line = rule.explain()
    assert "[penalty]" in line
    assert "not triggered" in line


def test_the_audit_ends_with_a_reconcilable_total():
    comp = ScoreComponent(
        category="c",
        weight=100.0,
        rules=[threshold_rule("a", "d", _m("x", 5.0), points=100.0, threshold=1.0)],
    )
    score = CompositeScore(
        candidate_id="c", run_id="r", methodology_version="v", scored_at=_ts(), components=[comp]
    )
    lines = score.audit_lines()
    assert lines[-1].startswith("TOTAL:")
    assert "100/100" in lines[-1]
    assert "UNCALIBRATED" in lines[-1]


# --- calibration -----------------------------------------------------------


def test_every_score_is_stamped_uncalibrated_by_default():
    """docs/PRODUCT_STANCE.md — no feature has cleared out-of-sample validation."""
    score = CompositeScore(
        candidate_id="c", run_id="r", methodology_version="v", scored_at=_ts(), components=[]
    )
    assert score.calibration_status is CalibrationStatus.UNCALIBRATED


def test_classification_bands_are_configurable_and_cover_every_score(methodology):
    for value, expected in [
        (95.0, "EXCEPTIONAL"),
        (85.0, "HIGH_CONVICTION"),
        (75.0, "GOOD"),
        (65.0, "WATCHLIST"),
        (10.0, "REJECT"),
        (0.0, "REJECT"),
    ]:
        assert methodology.classification.classify(value).label == expected


def _ts():
    from datetime import UTC, datetime

    return datetime(2026, 8, 5, 16, 0, tzinfo=UTC)


def test_score_rule_is_immutable_about_its_own_arithmetic():
    """points_possible of a penalty stays zero so it cannot inflate a category."""
    rule = ScoreRule(
        rule_id="p", description="d", points_awarded=-5.0, points_possible=0.0
    )
    assert rule.is_penalty
    assert rule.counted_possible == 0.0
