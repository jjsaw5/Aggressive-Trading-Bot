"""Layer-2 conviction gate — built to be RED, wired so it CAN go green.

The machinery exists before the feature that needs it (the auditor's ask): the
gate encodes, in code, every condition under which the platform would be allowed
to display CALIBRATED conviction. Today all of them fail — the feature registries
are empty (seven pre-registered/OOS tests, all null) and no calibration sample
exists — so the gate is red, and `conviction_status` stays UNCALIBRATED. If a
future feature ever clears the validation harness AND live calibration holds up,
the gate flips on its own evidence. Nothing else can flip it: the status is
derived here, never asserted anywhere.

Criteria (spec §6/§11, all must pass):
  1. VALIDATED FEATURE — at least one feature carries data-derived weight in a
     committed registry (conviction_require_validated).
  2. CALIBRATION SAMPLE — >= MIN_DECISIVE resolved, decisive outcomes graded on
     real marks (validation_grade == "real_marks").
  3. BRIER — POP forecast Brier score <= calibration_brier_max.
  4. DISCRIMINATION — score-vs-P&L Spearman > calibration_spearman_min.
  5. PER-REGIME — decisive outcomes span >= 2 vol regimes with >= MIN_PER_REGIME
     each (calibration_per_regime): a single-regime green is a bull-sample trap.

`sizing_requires_green_gate`: any future conviction-based sizing must consult
`gate.sizing_boost_allowed` — red gate, no boost, no exceptions.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

from pydantic import BaseModel, Field

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

MIN_DECISIVE = 30
MIN_PER_REGIME = 10

_REGISTRY_PATHS = ("docs/feature_registry.json", "docs/flow_feature_registry.json")


class GateCriterion(BaseModel):
    name: str
    passed: bool
    detail: str


class ConvictionGate(BaseModel):
    green: bool = False
    criteria: list[GateCriterion] = Field(default_factory=list)
    note: str = ""

    @property
    def sizing_boost_allowed(self) -> bool:
        return self.green and settings.sizing_requires_green_gate is not None


def _registry_validated() -> tuple[bool, str]:
    """Does any committed feature registry carry a validated, weighted feature?"""
    found: list[str] = []
    for path in _REGISTRY_PATHS:
        if not os.path.exists(path):
            continue
        try:
            reg = json.load(open(path))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("conviction_gate_registry_unreadable", path=path, error=str(exc))
            continue
        if reg.get("any_validated") and reg.get("weights"):
            found.extend(reg["weights"])
    if found:
        return True, f"validated features with weight: {', '.join(sorted(found))}"
    return False, (
        "no feature has cleared the out-of-sample validation gate "
        "(registries empty — 7 pre-registered/OOS tests, all null)"
    )


def evaluate_conviction_gate(scorecard=None) -> ConvictionGate:
    """Evaluate every criterion against the registries + a calibration Scorecard
    (app.analytics.calibration). Pass scorecard=None when no calibration data is
    loaded — the sample criteria then fail honestly."""
    crits: list[GateCriterion] = []

    ok, detail = _registry_validated()
    crits.append(GateCriterion(name="validated_feature", passed=ok, detail=detail))

    n_dec = getattr(scorecard, "n_decisive", 0) or 0
    grade = getattr(scorecard, "validation_grade", "insufficient")
    sample_ok = n_dec >= MIN_DECISIVE and grade == "real_marks"
    crits.append(GateCriterion(
        name="calibration_sample", passed=sample_ok,
        detail=f"n_decisive={n_dec} (need >={MIN_DECISIVE}), grade={grade} (need real_marks)",
    ))

    brier = getattr(scorecard, "brier_score", None)
    brier_ok = brier is not None and brier <= settings.calibration_brier_max
    crits.append(GateCriterion(
        name="brier", passed=brier_ok,
        detail=f"brier={brier} (need <={settings.calibration_brier_max})",
    ))

    sp = getattr(scorecard, "score_pnl_spearman", None)
    sp_ok = sp is not None and sp > settings.calibration_spearman_min
    crits.append(GateCriterion(
        name="discrimination", passed=sp_ok,
        detail=f"score_pnl_spearman={sp} (need >{settings.calibration_spearman_min})",
    ))

    if settings.calibration_per_regime:
        regimes = [g for g in (getattr(scorecard, "by_vol_regime", None) or [])
                   if g.n >= MIN_PER_REGIME]
        reg_ok = len(regimes) >= 2
        crits.append(GateCriterion(
            name="per_regime", passed=reg_ok,
            detail=(f"{len(regimes)} regime(s) with n>={MIN_PER_REGIME} "
                    "(need >=2 — a single-regime green is a bull-sample trap)"),
        ))

    green = all(c.passed for c in crits)
    return ConvictionGate(
        green=green, criteria=crits,
        note=(
            "GREEN: conviction may display as CALIBRATED." if green else
            "RED: conviction stays UNCALIBRATED. The gate flips only on its own "
            "evidence — a validated registry feature plus live, per-regime, "
            "real-marks calibration. It cannot be flipped by hand."
        ),
    )


@lru_cache(maxsize=1)
def get_conviction_gate() -> ConvictionGate:
    """Process-cached gate over the committed registries + the current calibration
    warehouse. Refresh with `refresh_conviction_gate()` after outcomes resolve."""
    scorecard = None
    try:
        from app.analytics.calibration import build_scorecard
        from app.db import repository
        snaps, outs = repository.fetch_calibration_data(limit=2000)
        if snaps:
            scorecard = build_scorecard(snaps, outs)
    except Exception as exc:  # noqa: BLE001 — no warehouse yet = criteria fail honestly
        log.warning("conviction_gate_scorecard_unavailable", error=str(exc))
    return evaluate_conviction_gate(scorecard)


def refresh_conviction_gate() -> ConvictionGate:
    get_conviction_gate.cache_clear()
    return get_conviction_gate()
