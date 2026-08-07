"""Score records — built so every point can be audited back to a measurement.

The spec's requirement is exact: *"Store the sub-score AND the measurements that
generated it... I want to be able to audit every point."*

So a score is not a number with a note attached. It is a tree:

    CompositeScore
      └─ ScoreComponent (one per category, e.g. technical_setup 17/20)
           └─ ScoreRule   (trend_aligned +5, measured value 4.2 vs threshold 1.0)

Every `ScoreRule` records the value it saw, the threshold it compared against,
and whether it MEASURED or ABSTAINED. Reconstructing the total from the leaves
is a test (`test_scoring_audit.py`), not a promise.

**Abstention.** A rule with no input abstains. Its points leave *both* sides of
the fraction, so the component reports 12/14 rather than 12/20, and the
composite is renormalised to 100 with `input_coverage` stated alongside. Scoring
a missing input as zero would make "we could not measure this" indistinguishable
from "we measured this and it was bad", which is precisely the confusion the
honesty rules exist to prevent.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, computed_field

from app.multiagent.models.enums import (
    CalibrationStatus,
    Classification,
    MeasurementStatus,
)
from app.multiagent.models.measurements import Measurement


class ScoreRule(BaseModel):
    """One deterministic rule and the measurement that drove it."""

    rule_id: str
    description: str

    points_awarded: float
    points_possible: float
    status: MeasurementStatus = MeasurementStatus.MEASURED

    # The evidence for the award. `measurement` is the input; `threshold` and
    # `comparison` state what it was tested against.
    measurement: Measurement | None = None
    threshold: float | None = None
    comparison: str = ""     # e.g. ">=", "<", "in band"
    detail: str = ""

    @property
    def abstained(self) -> bool:
        return self.status is MeasurementStatus.ABSTAINED

    @property
    def counted_possible(self) -> float:
        """Points this rule contributes to the denominator.

        Zero when abstained — that is the whole mechanism.
        """
        return 0.0 if self.abstained else self.points_possible

    @property
    def is_penalty(self) -> bool:
        """A penalty carries no possible points: it can subtract, never add."""
        return self.points_possible == 0.0

    def explain(self) -> str:
        if self.abstained:
            why = (
                self.measurement.absence_reason.value
                if self.measurement and self.measurement.absence_reason
                else "no input"
            )
            return (
                f"{self.rule_id}: ABSTAINED ({why}) — "
                f"{self.points_possible:g} pts removed from denominator"
            )

        val = "n/a"
        if self.measurement and self.measurement.present:
            val = f"{self.measurement.value:g}{self.measurement.unit}"

        if self.is_penalty:
            # Penalties read differently: "not triggered" is the good outcome and
            # printing it as "+0/0" invites the reader to think a rule failed.
            state = (
                f"TRIGGERED {self.points_awarded:g}"
                if self.points_awarded != 0
                else "not triggered"
            )
            thr = f", limit {self.threshold:g}" if self.threshold is not None else ""
            return f"{self.rule_id}: [penalty] {state} (measured {val}{thr})"

        thr = f" {self.comparison} {self.threshold:g}" if self.threshold is not None else ""
        sign = "+" if self.points_awarded >= 0 else ""
        return (
            f"{self.rule_id}: {sign}{self.points_awarded:g}/{self.points_possible:g} "
            f"(measured {val}{thr})"
        )


class ScoreComponent(BaseModel):
    """One of the eight categories."""

    category: str
    weight: float                     # the category's share of 100
    rules: list[ScoreRule] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def points_awarded(self) -> float:
        """Sum of awards, clamped to [0, available].

        Clamped because penalties are allowed to cancel credits but must not
        drive a category negative and silently subtract from other categories.
        """
        raw = sum(r.points_awarded for r in self.rules if not r.abstained)
        return round(max(0.0, min(raw, self.points_available)), 3)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def points_available(self) -> float:
        """Denominator: possible points from rules that actually ran.

        Penalty rules carry `points_possible = 0`, so they can subtract without
        inflating the denominator.
        """
        return round(sum(r.counted_possible for r in self.rules), 3)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def abstained(self) -> bool:
        """True when nothing in the category could be measured."""
        return bool(self.rules) and self.points_available == 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def coverage(self) -> float | None:
        """Share of the category's weight that had live inputs."""
        if self.weight <= 0:
            return None
        return round(min(1.0, self.points_available / self.weight), 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def normalized(self) -> float | None:
        """Category score restated out of its full weight, or None if abstained.

        This is what the composite sums. A category measured 12/14 contributes
        as if it were (12/14) x 20 — an honest extrapolation from what was
        measurable, with the extrapolation visible via `coverage`.
        """
        if self.points_available == 0.0:
            return None
        return round(self.points_awarded / self.points_available * self.weight, 3)

    def explain(self) -> list[str]:
        return [r.explain() for r in self.rules]


class CompositeScore(BaseModel):
    """The final 0-100 figure and its full derivation."""

    candidate_id: str
    run_id: str
    methodology_version: str
    scored_at: datetime

    components: list[ScoreComponent] = Field(default_factory=list)

    # Stamped on every score. `docs/PRODUCT_STANCE.md` — no feature has cleared
    # out-of-sample validation, so this reads UNCALIBRATED and the report says so.
    calibration_status: CalibrationStatus = CalibrationStatus.UNCALIBRATED

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measured_weight(self) -> float:
        """Total category weight that had live inputs."""
        return round(sum(c.weight for c in self.components if not c.abstained), 3)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def input_coverage(self) -> float:
        """Share of the full 100 points that could actually be measured.

        Reported next to every score. A 78 at 0.55 coverage and a 78 at 1.0
        coverage are different claims and the report never conflates them.
        """
        total = sum(c.weight for c in self.components)
        if total <= 0:
            return 0.0
        # Weight each category by how much of it was measurable, not just
        # whether any of it was.
        measurable = sum(c.weight * (c.coverage or 0.0) for c in self.components)
        return round(measurable / total, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def raw_points(self) -> float:
        """Points earned across measured categories, on the original 100 scale."""
        return round(sum(c.normalized for c in self.components if c.normalized is not None), 3)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def score(self) -> float:
        """The headline 0-100 number.

        Renormalised over measured weight so an abstained category does not
        silently cap the score at 90. When every category is measured this is
        exactly `raw_points`.
        """
        if self.measured_weight <= 0:
            return 0.0
        return round(self.raw_points / self.measured_weight * 100.0, 2)

    def component(self, category: str) -> ScoreComponent | None:
        for c in self.components:
            if c.category == category:
                return c
        return None

    def breakdown(self) -> dict[str, str]:
        """Category -> "17/20" style summary, abstentions named as such."""
        out: dict[str, str] = {}
        for c in self.components:
            if c.abstained:
                out[c.category] = f"ABSTAINED (0 of {c.weight:g} measurable)"
            else:
                out[c.category] = f"{c.points_awarded:g}/{c.points_available:g} (weight {c.weight:g})"
        return out

    def audit_lines(self) -> list[str]:
        """Every point, traced. This is the auditability requirement, realised."""
        lines: list[str] = []
        for c in self.components:
            header = (
                f"{c.category}: ABSTAINED"
                if c.abstained
                else f"{c.category}: {c.points_awarded:g}/{c.points_available:g} "
                f"-> {c.normalized:g}/{c.weight:g} weighted"
            )
            lines.append(header)
            lines.extend(f"    {line}" for line in c.explain())
            lines.extend(f"    note: {n}" for n in c.notes)
        lines.append(
            f"TOTAL: {self.raw_points:g} of {self.measured_weight:g} measured weight "
            f"-> {self.score:g}/100 (coverage {self.input_coverage:.0%}, {self.calibration_status.value})"
        )
        return lines


class ScoredCandidate(BaseModel):
    """A candidate plus its score and disposition. What ranking sorts."""

    candidate_id: str
    run_id: str
    ticker: str
    score: CompositeScore
    classification: Classification
    classification_name: str
    rank: int | None = None
    is_ranked: bool = False
    rejection_codes: list[str] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=list)
