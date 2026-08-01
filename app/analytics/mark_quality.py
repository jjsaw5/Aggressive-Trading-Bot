"""How well-observed a grade actually was — carried WITH the grade.

Pre-flight item P7 (reviewer Ruling 2). The audit packet measured 31% session
coverage with a 53-minute maximum gap on a *liquid* 0DTE contract, and the
ruling's response was that this number must travel with every grade it affects
rather than living in a memo:

    "The 53-minute-gap number the packet flags must travel with every grade it
    affects, not live in a memo."

WHY IT MATTERS, precisely. UW minute bars are trade-driven: a bar exists only for
a minute that printed. The replay holds through gaps and never interpolates, so
an exit that triggered and reversed inside a gap is **missed, not mispriced**.
The bias has a direction — trades look like they ran LONGER than they did, and
stop-outs are under-reported. A grade with a 53-minute hole and a grade with
full coverage are not the same measurement, and pooling them silently would
understate how often the managed policy actually fired.

Ruling 2 also set a quantitative re-enable bar for 0DTE, which lives here so the
threshold and its rationale sit next to the measurement it gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)
RTH_MINUTES = 390  # 09:30–16:00 ET

# A grade whose largest blind spot exceeds this is flagged `low`. Chosen by the
# reviewer, not derived: 15 minutes is long enough for a 50%-of-debit move on a
# short-dated option to happen and fully reverse unobserved.
LOW_CONFIDENCE_GAP_MINUTES = 15

# --- 0DTE re-enable bar (Ruling 2) -------------------------------------------
# 0DTE stays suspended until minute data demonstrably supports it. 31% coverage
# with 53-minute gaps is disqualifying for a bucket whose entire trade lives
# inside one session: there, a 53-minute hole is not a bound on the answer, it
# is a hole in it.
ZERO_DTE_MIN_COVERAGE_PCT = 0.80
ZERO_DTE_MAX_GAP_MINUTES = 5


@dataclass(frozen=True)
class MarkQuality:
    """Observation quality for one graded decision."""

    n_marks: int
    # Priced minutes / RTH minutes in the graded window. None when the window
    # contains no RTH minutes at all (the denominator would be zero, and 0/0 is
    # not 0%).
    coverage_pct: float | None
    # Largest run of consecutive unobserved minutes inside the window. None when
    # fewer than two marks exist — a gap needs two edges to be measured, and
    # reporting 0 would claim perfect continuity from a single observation.
    max_gap_minutes: int | None
    confidence: str  # high | low | unknown

    @property
    def is_low_confidence(self) -> bool:
        return self.confidence != "high"


def _rth_minutes_between(start: datetime, end: datetime) -> int:
    """RTH minutes from `start` to `end`, walking whole sessions in ET.

    Deliberately simple: it counts weekday RTH minutes and does not consult a
    holiday calendar. A market holiday therefore inflates the denominator and
    UNDERSTATES coverage — an error in the conservative direction, which is the
    right way for a data-quality metric to be wrong.
    """
    if end <= start:
        return 0
    s, e = start.astimezone(_ET), end.astimezone(_ET)
    total = 0
    day = s.date()
    while day <= e.date():
        if day.weekday() < 5:
            open_ = datetime.combine(day, _RTH_OPEN, tzinfo=_ET)
            close = datetime.combine(day, _RTH_CLOSE, tzinfo=_ET)
            lo, hi = max(open_, s), min(close, e)
            if hi > lo:
                total += int((hi - lo).total_seconds() // 60)
        day = day.fromordinal(day.toordinal() + 1)
    return total


def assess(
    minutes: list[datetime], *, start: datetime | None = None, end: datetime | None = None
) -> MarkQuality:
    """Quality of the observation set `minutes` over the window [start, end].

    `minutes` are the timestamps that actually priced — the replay's own view,
    not the vendor's raw response, so the metric describes the grade rather than
    the download.
    """
    if not minutes:
        return MarkQuality(0, None, None, "unknown")

    ordered = sorted(minutes)
    lo = start or ordered[0]
    hi = end or ordered[-1]

    expected = _rth_minutes_between(lo, hi)
    coverage = round(min(1.0, len(ordered) / expected), 4) if expected > 0 else None

    gap: int | None = None
    if len(ordered) >= 2:
        gap = 0
        for a, b in zip(ordered[:-1], ordered[1:], strict=True):
            # Consecutive minutes are a 0-minute gap, not a 1-minute one.
            gap = max(gap, int((b - a).total_seconds() // 60) - 1)

    if gap is None:
        confidence = "unknown"  # a single mark cannot evidence continuity
    elif gap > LOW_CONFIDENCE_GAP_MINUTES:
        confidence = "low"
    else:
        confidence = "high"

    return MarkQuality(len(ordered), coverage, gap, confidence)


def meets_zero_dte_bar(q: MarkQuality) -> tuple[bool, str]:
    """(passes, reason) against Ruling 2's 0DTE re-enable bar.

    Both conditions are required. An unmeasurable one FAILS: the bar exists to
    demonstrate coverage, and "we could not tell" is not a demonstration.
    """
    if q.coverage_pct is None:
        return False, "coverage unmeasurable (no RTH minutes in window)"
    if q.max_gap_minutes is None:
        return False, "gap unmeasurable (fewer than 2 marks)"
    if q.coverage_pct < ZERO_DTE_MIN_COVERAGE_PCT:
        return False, (
            f"coverage {q.coverage_pct:.0%} < {ZERO_DTE_MIN_COVERAGE_PCT:.0%} of RTH"
        )
    if q.max_gap_minutes > ZERO_DTE_MAX_GAP_MINUTES:
        return False, (
            f"max gap {q.max_gap_minutes}min > {ZERO_DTE_MAX_GAP_MINUTES}min"
        )
    return True, (
        f"coverage {q.coverage_pct:.0%}, max gap {q.max_gap_minutes}min — "
        "meets the re-enable bar"
    )
