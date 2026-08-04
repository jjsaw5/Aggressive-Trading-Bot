"""Observation quality, and the 0DTE bar it gates.

Pre-flight P7 (reviewer Ruling 2). The audit measured 31% session coverage with
a 53-minute maximum gap on a liquid 0DTE contract. The ruling's response was that
the number must travel with every grade it affects rather than living in a memo,
and that 0DTE stays suspended behind a quantitative bar.

The distinction these tests protect: an exit that triggered and reversed inside
a gap is **missed, not mispriced**. So the error is directional — trades look
longer-held than they were, and stop-outs are under-reported. A grade with a
53-minute hole and a grade with full coverage are not the same measurement.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.analytics.mark_quality import (
    LOW_CONFIDENCE_GAP_MINUTES,
    ZERO_DTE_MAX_GAP_MINUTES,
    ZERO_DTE_MIN_COVERAGE_PCT,
    assess,
    meets_zero_dte_bar,
)

# 13:30Z = 09:30 ET, the RTH open.
_OPEN = datetime(2026, 8, 3, 13, 30, tzinfo=UTC)


def _minutes(*offsets: int) -> list[datetime]:
    return [_OPEN + timedelta(minutes=m) for m in offsets]


def _dense(n: int) -> list[datetime]:
    return _minutes(*range(n))


# --- Gaps ---------------------------------------------------------------------
def test_consecutive_minutes_are_a_zero_minute_gap() -> None:
    # Adjacent bars are continuous, not one minute apart in blind-spot terms.
    assert assess(_dense(10)).max_gap_minutes == 0


def test_the_largest_gap_is_reported_not_the_average() -> None:
    # One long hole is the risk; averaging would hide it behind dense stretches.
    q = assess(_minutes(0, 1, 2, 55, 56))
    assert q.max_gap_minutes == 52


def test_a_single_mark_cannot_evidence_continuity() -> None:
    # Reporting 0 here would claim perfect coverage from one observation.
    q = assess(_minutes(0))
    assert q.max_gap_minutes is None and q.confidence == "unknown"


def test_no_marks_at_all_is_unknown_not_zero_coverage() -> None:
    q = assess([])
    assert q.n_marks == 0 and q.coverage_pct is None and q.confidence == "unknown"


# --- Confidence flag ----------------------------------------------------------
def test_a_gap_past_the_threshold_downgrades_confidence() -> None:
    q = assess(_minutes(0, LOW_CONFIDENCE_GAP_MINUTES + 2))
    assert q.confidence == "low" and q.is_low_confidence


def test_a_gap_within_the_threshold_stays_high() -> None:
    q = assess(_minutes(0, LOW_CONFIDENCE_GAP_MINUTES))
    assert q.confidence == "high" and not q.is_low_confidence


def test_the_audited_53_minute_gap_would_be_flagged_low() -> None:
    # THE case from the packet, by name.
    assert assess(_minutes(0, 54)).confidence == "low"


# --- Coverage -----------------------------------------------------------------
def test_full_rth_coverage_reads_as_one() -> None:
    q = assess(_dense(390), start=_OPEN, end=_OPEN + timedelta(minutes=390))
    assert q.coverage_pct == pytest.approx(1.0)


def test_the_audited_31_percent_reproduces() -> None:
    # 123 marks over a 402-minute window — the packet's measured figure.
    marks = _minutes(*range(0, 402, 402 // 123))[:123]
    q = assess(marks, start=_OPEN, end=_OPEN + timedelta(minutes=402))
    assert q.coverage_pct is not None and 0.28 < q.coverage_pct < 0.35


def test_coverage_is_capped_at_one() -> None:
    # More marks than RTH minutes (extended-hours prints) must not read >100%.
    q = assess(_dense(60), start=_OPEN, end=_OPEN + timedelta(minutes=30))
    assert q.coverage_pct == pytest.approx(1.0)


def test_a_window_with_no_rth_minutes_has_unmeasurable_coverage() -> None:
    # 0/0 is not 0%. A Saturday window cannot produce a coverage figure.
    sat = datetime(2026, 8, 8, 14, 0, tzinfo=UTC)
    q = assess([sat, sat + timedelta(minutes=5)], start=sat, end=sat + timedelta(minutes=10))
    assert q.coverage_pct is None


def test_an_overnight_window_counts_only_rth_minutes() -> None:
    # 09:30 day 1 -> 09:30 day 2 spans 24h but only ~390 tradeable minutes.
    q = assess(_dense(390), start=_OPEN, end=_OPEN + timedelta(hours=24))
    assert q.coverage_pct is not None and q.coverage_pct > 0.9


# --- The 0DTE re-enable bar ---------------------------------------------------
def test_the_audited_quality_fails_the_0dte_bar() -> None:
    """THE ruling. 31% / 53min is disqualifying for a same-session bucket."""
    marks = _minutes(*range(0, 402, 4))[:100]
    marks.append(_OPEN + timedelta(minutes=460))  # a 53-minute hole
    q = assess(marks, start=_OPEN, end=_OPEN + timedelta(minutes=470))
    ok, reason = meets_zero_dte_bar(q)
    assert ok is False and reason


def test_dense_coverage_passes_the_bar() -> None:
    q = assess(_dense(390), start=_OPEN, end=_OPEN + timedelta(minutes=390))
    ok, reason = meets_zero_dte_bar(q)
    assert ok is True and "meets the re-enable bar" in reason


def test_good_coverage_with_one_long_hole_still_fails() -> None:
    # Both conditions are required; a single blind spot is enough to sink it,
    # because that is exactly where a same-session exit hides.
    marks = _dense(350) + [_OPEN + timedelta(minutes=380)]
    q = assess(marks, start=_OPEN, end=_OPEN + timedelta(minutes=390))
    ok, reason = meets_zero_dte_bar(q)
    assert ok is False and "max gap" in reason


def test_unmeasurable_quality_fails_rather_than_passes() -> None:
    # The bar exists to DEMONSTRATE coverage; "we could not tell" is not a
    # demonstration, so it must not pass by default.
    ok, reason = meets_zero_dte_bar(assess([]))
    assert ok is False and "unmeasurable" in reason


def test_the_thresholds_are_the_ones_the_ruling_set() -> None:
    assert ZERO_DTE_MIN_COVERAGE_PCT == 0.80
    assert ZERO_DTE_MAX_GAP_MINUTES == 5


# --- Wired into the gate ------------------------------------------------------
def test_the_0dte_rejection_states_the_quantitative_bar() -> None:
    """The rejection must say WHY, in numbers — the memo problem again."""
    from app.config import settings
    from app.domain.enums import DTECategory
    from app.shortduration.capture_gates import bucket_suspended

    # Amendment 3 moved 0DTE to observation-only, so it is not suspended by
    # default. The rejection MESSAGE is what this test is about — that it states
    # the bar in numbers rather than gesturing at a memo — so suspension is
    # configured explicitly here.
    original = settings.capture_suspended_buckets
    settings.capture_suspended_buckets = "0dte"
    try:
        r = bucket_suspended(DTECategory.ZERO_DTE)
    finally:
        settings.capture_suspended_buckets = original
    assert r is not None
    assert "80%" in r.detail and "5min" in r.detail
    assert "52min" in r.detail  # the measured value that failed it
