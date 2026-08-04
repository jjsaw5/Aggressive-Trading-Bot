"""Amendment 3: 0DTE is captured and paper-traded, never calibrated.

Ruling 2 suspended 0DTE because its GRADES are uninterpretable — 31% session
coverage with a 52-minute maximum gap on trade-driven minute bars. Suspension
dropped the setup before scoring, so no record existed at all, which also threw
away the signal data the logic needs to be developed against.

Observation-only splits those: the bucket produces candidates and paper trades,
and its outcomes are quarantined out of the calibration corpus. These tests pin
the quarantine, because that is the whole basis on which capture is allowed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.analytics.calibration import (
    _drop_observation_only,
    _is_degraded_short_duration,
    gradeable_outcomes,
)
from app.domain.enums import Direction, DTECategory, StrategyType
from app.domain.outcomes import DecisionOutcome, DecisionSnapshot, OutcomeResult
from app.domain.trades import RiskPlan, TradePlan
from app.shortduration.capture_gates import (
    bucket_suspended,
    is_observation_only,
    observation_only_note,
)

NOW = datetime(2026, 8, 4, 14, 0, tzinfo=UTC)


def _snap(decision_id: str, bucket: str, dte: int | None) -> DecisionSnapshot:
    return DecisionSnapshot(
        decision_id=decision_id, scan_id=decision_id, symbol="SPY",
        direction=Direction.BULLISH, strategy=StrategyType.LONG_CALL,
        generated_at=NOW, composite_score=0.5,
        entry_net_per_share=1.0, max_loss_usd=100.0, contracts=1,
        dte_at_entry=dte, dte_bucket=bucket,
        trade_plan=TradePlan(
            symbol="SPY", direction=Direction.BULLISH,
            strategy=StrategyType.LONG_CALL, legs=[], net_debit=100.0, contracts=1,
            risk=RiskPlan(max_loss_usd=100.0, account_risk_pct=0.05,
                          profit_target_pct=0.5, stop_loss_pct=0.5),
        ),
    )


def _out(decision_id: str, confidence: str) -> DecisionOutcome:
    return DecisionOutcome(
        decision_id=decision_id, symbol="SPY", horizon_label="expiry",
        resolved_at=NOW, result=OutcomeResult.WIN, grade_confidence=confidence,
    )


# --- The bucket is captured, not dropped --------------------------------------
def test_0dte_is_no_longer_suspended() -> None:
    """Suspension dropped the setup pre-scoring; no record existed at all."""
    assert bucket_suspended(DTECategory.ZERO_DTE) is None


def test_0dte_is_observation_only() -> None:
    assert is_observation_only(DTECategory.ZERO_DTE) is True


def test_the_tradeable_buckets_are_not_observation_only() -> None:
    assert is_observation_only(DTECategory.SHORT_DTE) is False
    assert is_observation_only(DTECategory.MEDIUM_DTE) is False


def test_the_note_states_the_bar_in_numbers() -> None:
    """The memo problem again: say WHY, quantitatively, wherever it is shown."""
    note = observation_only_note(DTECategory.ZERO_DTE)
    assert "OBSERVATION ONLY" in note
    assert "80%" in note and "5min" in note and "52min" in note
    assert observation_only_note(DTECategory.SHORT_DTE) == ""


# --- ...and quarantined from calibration --------------------------------------
def test_0dte_decisions_are_dropped_from_the_corpus() -> None:
    kept, n = _drop_observation_only([
        _snap("a", "0dte", 0), _snap("b", "1-5dte", 3), _snap("c", "medium", 30),
    ])
    assert n == 1
    assert [s.decision_id for s in kept] == ["b", "c"]


def test_a_one_dte_short_dte_row_survives() -> None:
    """THE reason the filter matches on the recorded bucket, not on the integer.

    The 0DTE selector admits dte 0 OR 1, and the 1-5DTE selector starts at 1. A
    filter keyed on `dte_at_entry` would drop this legitimate 1-5DTE decision.
    """
    kept, n = _drop_observation_only([_snap("a", "1-5dte", 1), _snap("b", "0dte", 1)])
    assert n == 1
    assert [s.decision_id for s in kept] == ["a"]


def test_an_unrecorded_bucket_is_kept() -> None:
    """Pre-Amendment-3 and funnel rows carry no bucket. Absent is not evidence of
    membership — dropping them would silently shrink the historical corpus."""
    kept, n = _drop_observation_only([_snap("a", "", 0), _snap("b", "", None)])
    assert n == 0 and len(kept) == 2


# --- The general grade-confidence quarantine ----------------------------------
@pytest.mark.parametrize("confidence", ["low", "unknown"])
def test_a_poorly_observed_grade_is_excluded(confidence: str) -> None:
    kept, n = gradeable_outcomes([_out("a", confidence)])
    assert n == 1 and kept == []


def test_a_well_observed_grade_is_kept() -> None:
    kept, n = gradeable_outcomes([_out("a", "high")])
    assert n == 0 and len(kept) == 1


def test_pre_p7_grades_without_the_measurement_are_kept() -> None:
    """Empty is the pre-P7 default: unknown-but-not-known-bad. Excluding it would
    discard the entire corpus that predates the measurement."""
    kept, n = gradeable_outcomes([_out("a", "")])
    assert n == 0 and len(kept) == 1


def test_the_two_quarantines_are_independent() -> None:
    """Bucket and confidence catch different cases, which is why both exist: a
    0DTE grade from DAILY marks carries the empty confidence string and would
    pass the confidence filter while being exactly the uninterpretable case."""
    kept_o, n_o = gradeable_outcomes([_out("a", "")])
    kept_s, n_s = _drop_observation_only([_snap("a", "0dte", 0)])
    assert n_o == 0 and len(kept_o) == 1, "confidence filter alone lets it through"
    assert n_s == 1 and kept_s == [], "the bucket filter is what catches it"


# --- The version regex fixed alongside ----------------------------------------
@pytest.mark.parametrize(
    ("version", "degraded"),
    [
        ("sd-scoring-2026.07-v2", True),
        ("sd-scoring-2026.07-v3", False),
        ("sd-scoring-2026.08-v3.1", False),
        ("sd-scoring-2026.08-v4.1", False),
        # The case the old `-v(\\d+)$` pattern silently admitted as undegraded.
        ("sd-scoring-2025.01-v2.5", True),
        ("", False),
    ],
)
def test_dotted_versions_are_parsed_for_degradation(version: str, degraded: bool) -> None:
    assert _is_degraded_short_duration(version) is degraded


def test_the_configured_model_version_is_the_amended_one() -> None:
    from app.config import settings

    assert settings.scoring_model_version == "sd-scoring-2026.08-v4.1"


def test_suspended_and_observation_only_are_disjoint() -> None:
    """A bucket cannot be both dropped pre-scoring and captured-but-quarantined."""
    from app.config import settings

    susp = {s.strip() for s in settings.capture_suspended_buckets.split(",") if s.strip()}
    obs = {s.strip() for s in settings.capture_observation_only_buckets.split(",") if s.strip()}
    assert not (susp & obs), f"buckets in both states: {susp & obs}"
