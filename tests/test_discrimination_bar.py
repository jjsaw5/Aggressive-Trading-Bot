"""The discrimination criterion must not pass on noise.

Regression for a gate that reported PASS at Spearman +0.026 — statistically
indistinguishable from zero. A gate that grants permission on noise is worse
than no gate, because it launders a coin flip into confidence.
"""

from __future__ import annotations

import random

from app.analytics.metrics import spearman, spearman_ci
from app.config import get_settings
from app.shortduration.conviction_gate import evaluate_conviction_gate


class _Card:
    """Minimal scorecard stand-in carrying only what the gate reads."""

    def __init__(self, sp, ci, n, *, decisive=200, brier=0.10, grade="real_marks"):
        self.score_pnl_spearman = sp
        self.score_pnl_spearman_ci = ci
        self.score_pnl_n = n
        self.n_decisive = decisive
        self.brier_score = brier
        self.validation_grade = grade
        self.by_vol_regime = []


def _crit(card, name):
    g = evaluate_conviction_gate(card)
    return next(c for c in g.criteria if c.name == name)


# --- The bootstrap interval ---------------------------------------------------
def test_ci_brackets_a_strong_correlation_away_from_zero() -> None:
    rng = random.Random(7)
    xs = [rng.random() for _ in range(200)]
    ys = [x + rng.gauss(0, 0.25) for x in xs]  # strongly related
    ci = spearman_ci(xs, ys)
    assert ci is not None
    lo, hi = ci
    assert lo > 0.3 and hi > lo
    assert lo <= spearman(xs, ys) <= hi


def test_ci_includes_zero_for_unrelated_series() -> None:
    # The case that matters: no relationship at all. The point estimate wanders
    # off zero by chance; the interval must not.
    rng = random.Random(11)
    xs = [rng.random() for _ in range(200)]
    ys = [rng.random() for _ in range(200)]
    ci = spearman_ci(xs, ys)
    assert ci is not None
    lo, hi = ci
    assert lo < 0.0 < hi


def test_ci_is_deterministic_across_runs() -> None:
    # A gate whose verdict flickers run-to-run on identical data is not a gate.
    rng = random.Random(3)
    xs = [rng.random() for _ in range(80)]
    ys = [x * 0.5 + rng.random() for x in xs]
    assert spearman_ci(xs, ys) == spearman_ci(xs, ys)


def test_ci_abstains_on_a_sample_too_small_to_bootstrap() -> None:
    assert spearman_ci([1, 2, 3, 4, 5], [2, 1, 4, 3, 5]) is None


# --- The gate criterion -------------------------------------------------------
def test_the_old_noise_level_pass_now_fails() -> None:
    # Exactly what production reported: +0.0257 with an interval straddling zero.
    c = _crit(_Card(0.0257, [-0.12, 0.17], 127), "discrimination")
    assert c.passed is False
    assert "not distinguishable from noise" in c.detail


def test_a_material_correlation_with_a_clean_interval_passes() -> None:
    c = _crit(_Card(0.22, [0.06, 0.37], 140), "discrimination")
    assert c.passed is True
    assert "0.22" in c.detail


def test_a_material_correlation_whose_interval_includes_zero_fails() -> None:
    # Big point estimate, but the sample can't rule out zero — not evidence.
    c = _crit(_Card(0.31, [-0.04, 0.58], 60), "discrimination")
    assert c.passed is False
    assert "includes zero" in c.detail


def test_too_few_priced_outcomes_cannot_be_judged() -> None:
    c = _crit(_Card(0.40, [0.10, 0.65], 12), "discrimination")
    assert c.passed is False
    assert "need >=" in c.detail


def test_a_missing_correlation_fails_rather_than_defaulting_open() -> None:
    c = _crit(_Card(None, None, 0), "discrimination")
    assert c.passed is False


def test_the_configured_bar_is_material_not_merely_positive() -> None:
    # Guards the regression directly: if this is ever set back toward zero, the
    # gate can be passed by noise again.
    assert get_settings().calibration_spearman_min >= 0.10
    assert get_settings().calibration_spearman_require_ci is True
