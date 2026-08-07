"""Rule primitives for the scoring engine.

Every award goes through one of these builders, so every award carries the
measurement that produced it, the threshold it was tested against, and whether
it was measured at all. There is no path to add points without leaving that
trail — `tests/multiagent/test_scoring_audit.py` reconstructs each total from
the leaves and fails if they disagree.

The abstention mechanic lives in `abstain()`: a rule with no input produces
`points_possible` on the record for display, but `counted_possible` of zero, so
its weight leaves the denominator. Scoring a missing input as zero would make
"could not measure" indistinguishable from "measured, and bad", which is the
confusion `CLAUDE.md` §4 exists to prevent.
"""

from __future__ import annotations

from app.multiagent.models.enums import MeasurementStatus
from app.multiagent.models.measurements import Measurement
from app.multiagent.models.scoring import ScoreRule


def abstain(rule_id: str, description: str, points: float, measurement: Measurement | None) -> ScoreRule:
    """A rule that could not run. Its points leave both sides of the fraction."""
    return ScoreRule(
        rule_id=rule_id,
        description=description,
        points_awarded=0.0,
        points_possible=points,
        status=MeasurementStatus.ABSTAINED,
        measurement=measurement,
        detail="input unavailable — points removed from the denominator, not scored zero",
    )


def threshold_rule(
    rule_id: str,
    description: str,
    measurement: Measurement,
    *,
    points: float,
    threshold: float,
    higher_is_better: bool = True,
    partial: bool = False,
    floor: float = 0.0,
) -> ScoreRule:
    """Award `points` when the measurement clears `threshold`.

    With `partial=True`, award proportionally between `floor` and `threshold`
    instead of all-or-nothing — appropriate where the quantity is continuous and
    a hair below the bar is materially different from well below it.

    **`floor` is not optional in spirit.** Scaling partial credit from zero
    means a measurement at its natural baseline collects most of the points: a
    relative volume of exactly 1.0x — dead average, the definition of no signal
    — would score 1.0/1.3 = 77% of the credit for "volume is strong". The floor
    is the value at which the rule should award nothing, so credit runs from
    "no signal" to "clears the bar" rather than from zero.
    """
    if not measurement.present:
        return abstain(rule_id, description, points, measurement)

    value = measurement.require()
    passes = value >= threshold if higher_is_better else value <= threshold

    if passes:
        awarded = points
        detail = ""
    elif partial and threshold != floor:
        ratio = (value - floor) / (threshold - floor)
        awarded = round(points * max(0.0, min(1.0, ratio)), 3)
        detail = f"partial credit scaled from {floor:g} (no credit) to {threshold:g} (full)"
    else:
        awarded = 0.0
        detail = ""

    return ScoreRule(
        rule_id=rule_id,
        description=description,
        points_awarded=awarded,
        points_possible=points,
        measurement=measurement,
        threshold=threshold,
        comparison=">=" if higher_is_better else "<=",
        detail=detail,
    )


def band_rule(
    rule_id: str,
    description: str,
    measurement: Measurement,
    *,
    points: float,
    low: float,
    high: float,
) -> ScoreRule:
    """Award `points` when the measurement sits inside [low, high]."""
    if not measurement.present:
        return abstain(rule_id, description, points, measurement)
    value = measurement.require()
    inside = low <= value <= high
    return ScoreRule(
        rule_id=rule_id,
        description=description,
        points_awarded=points if inside else 0.0,
        points_possible=points,
        measurement=measurement,
        threshold=low,
        comparison=f"in [{low:g}, {high:g}]",
        detail="" if inside else f"{value:g} is outside the band",
    )


def boolean_rule(
    rule_id: str,
    description: str,
    condition: bool | None,
    *,
    points: float,
    measurement: Measurement | None = None,
    detail: str = "",
) -> ScoreRule:
    """Award `points` when `condition` is True.

    `condition` is deliberately `bool | None`: None means the question could not
    be answered, and abstains. A tri-state boolean is the whole reason a neutral
    tape does not count as "fighting the tape".
    """
    if condition is None:
        return abstain(rule_id, description, points, measurement)
    return ScoreRule(
        rule_id=rule_id,
        description=description,
        points_awarded=points if condition else 0.0,
        points_possible=points,
        measurement=measurement,
        comparison="is true",
        detail=detail,
    )


def penalty_rule(
    rule_id: str,
    description: str,
    condition: bool | None,
    *,
    points: float,
    measurement: Measurement | None = None,
    threshold: float | None = None,
    detail: str = "",
) -> ScoreRule:
    """Subtract when `condition` is True.

    `points` is passed as a negative number from config. `points_possible` is
    zero: a penalty must be able to subtract without inflating the denominator,
    which would otherwise let adding a penalty *raise* a category's ceiling.
    """
    if condition is None:
        # A penalty that cannot be evaluated simply does not fire. It carries no
        # possible points, so there is nothing to remove from the denominator.
        return ScoreRule(
            rule_id=rule_id,
            description=description,
            points_awarded=0.0,
            points_possible=0.0,
            status=MeasurementStatus.ABSTAINED,
            measurement=measurement,
            detail="penalty condition unmeasurable — not applied",
        )
    return ScoreRule(
        rule_id=rule_id,
        description=description,
        points_awarded=points if condition else 0.0,
        points_possible=0.0,
        measurement=measurement,
        threshold=threshold,
        comparison="penalty" if condition else "",
        detail=detail if condition else "not triggered",
    )
