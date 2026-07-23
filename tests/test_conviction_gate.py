"""Layer-2 conviction gate — red by default, green only on full evidence."""

from __future__ import annotations

from app.analytics.calibration import Bucket, GroupStat, Scorecard  # noqa: F401
from app.shortduration.conviction_gate import (
    MIN_DECISIVE,
    evaluate_conviction_gate,
)


def _green_scorecard() -> Scorecard:
    return Scorecard(
        n_decisions=100, n_resolved=60, n_decisive=MIN_DECISIVE,
        brier_score=0.18, score_pnl_spearman=0.2, validation_grade="real_marks",
        by_vol_regime=[GroupStat(key="fair", n=15, win_rate=0.6),
                       GroupStat(key="rich", n=15, win_rate=0.5)],
    )


def test_gate_is_red_today_with_named_failures() -> None:
    # Empty registries + no calibration data -> every criterion fails, gate red.
    gate = evaluate_conviction_gate(scorecard=None)
    assert gate.green is False
    failed = {c.name for c in gate.criteria if not c.passed}
    assert "validated_feature" in failed  # registries are empty (7 nulls)
    assert "calibration_sample" in failed
    assert gate.sizing_boost_allowed is False
    assert "cannot be flipped by hand" in gate.note


def test_gate_stays_red_even_with_perfect_calibration_but_no_validated_feature() -> None:
    # The binding constraint: live calibration alone can NEVER flip the gate —
    # a validated registry feature is required (conviction earned, not asserted).
    gate = evaluate_conviction_gate(scorecard=_green_scorecard())
    assert gate.green is False
    by_name = {c.name: c for c in gate.criteria}
    assert by_name["validated_feature"].passed is False
    # Everything else passes — proving the machinery CAN go green on evidence.
    assert by_name["calibration_sample"].passed is True
    assert by_name["brier"].passed is True
    assert by_name["discrimination"].passed is True
    assert by_name["per_regime"].passed is True


def test_gate_goes_green_only_with_validated_registry(monkeypatch, tmp_path) -> None:
    # Write a synthetic validated registry and point the gate at it: with the full
    # calibration evidence AND a validated feature, the gate flips — by evidence.
    import json

    import app.shortduration.conviction_gate as cg

    reg = tmp_path / "feature_registry.json"
    reg.write_text(json.dumps({"any_validated": True, "weights": {"some_feature": 100.0}}))
    monkeypatch.setattr(cg, "_REGISTRY_PATHS", (str(reg),))
    gate = cg.evaluate_conviction_gate(scorecard=_green_scorecard())
    assert gate.green is True
    assert gate.sizing_boost_allowed is True


def test_single_regime_calibration_fails_per_regime_criterion() -> None:
    # A bull-sample green is the trap the per-regime criterion exists to block.
    card = _green_scorecard()
    card.by_vol_regime = [GroupStat(key="fair", n=30, win_rate=0.6)]
    gate = evaluate_conviction_gate(scorecard=card)
    by_name = {c.name: c for c in gate.criteria}
    assert by_name["per_regime"].passed is False
    assert gate.green is False


def test_engine_derives_uncalibrated_from_red_gate() -> None:
    # The scorer's conviction_status comes THROUGH the gate (red today).
    from app.domain.enums import DTECategory
    from app.shortduration.scoring.engine import score_candidate
    from app.shortduration.strategies.base import SetupContext
    from tests.test_sd_scoring import _NOW, _chain, _detection, _iv, _levels, _regime

    ctx = SetupContext(symbol="SPY", now=_NOW, regime=_regime(), levels=_levels(),
                       change_pct=0.8)
    card = score_candidate(ctx, _detection(dte=DTECategory.SHORT_DTE),
                           chain=_chain(), iv=_iv())
    assert card.conviction_status == "UNCALIBRATED"
