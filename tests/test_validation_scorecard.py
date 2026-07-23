"""Phase 2: the forward-outcome scorecard is the validation source.

It must prefer real-mark (option_marks / paper_trade) outcomes, report cost-net
P&L metrics + drawdown per structure and per vol-regime, grade its own
trustworthiness, and flag a sample too thin to calibrate.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.analytics.calibration import build_scorecard
from app.domain.enums import Direction, StrategyType
from app.domain.outcomes import (
    DecisionOutcome,
    DecisionSnapshot,
    DecisionSource,
    OutcomeResult,
)
from app.domain.trades import RiskPlan, TradePlan

_GEN = datetime(2026, 6, 1, tzinfo=UTC)


def _snap(did: str, *, iv_rank: float, score: float = 0.7, dte: int = 46) -> DecisionSnapshot:
    plan = TradePlan(
        symbol="AAA", direction=Direction.BULLISH, strategy=StrategyType.BULL_PUT_SPREAD,
        legs=[], net_debit=-100.0, contracts=1,
        risk=RiskPlan(max_loss_usd=400.0, max_profit_usd=100.0, account_risk_pct=0.05,
                      profit_target_pct=0.5, stop_loss_pct=0.5),
    )
    return DecisionSnapshot(
        decision_id=did, scan_id="s", symbol="AAA", source=DecisionSource.SCAN,
        direction=Direction.BULLISH, strategy=StrategyType.BULL_PUT_SPREAD, generated_at=_GEN,
        composite_score=score, probability_of_profit=0.7, iv_rank=iv_rank, entry_spot=100.0,
        entry_net_per_share=-1.0, max_loss_usd=400.0, max_profit_usd=100.0, contracts=1,
        expiration=date(2026, 7, 17), dte_at_entry=dte, trade_plan=plan,
    )


def _marks_outcome(did: str, pnl: float, day: int) -> DecisionOutcome:
    result = OutcomeResult.WIN if pnl > 0 else OutcomeResult.LOSS if pnl < 0 else OutcomeResult.SCRATCH
    return DecisionOutcome(
        decision_id=did, symbol="AAA", horizon_label=f"{day}d",
        resolved_at=datetime(2026, 6, 1 + day, tzinfo=UTC), elapsed_days=day, result=result,
        realized_pnl_usd=pnl, realized_pnl_gross_usd=pnl + 15, costs_usd=15.0,
        outcome_source="option_marks",
    )


def test_pre_v3_short_duration_decisions_are_hard_filtered() -> None:
    # The IV-rank bug degraded every pre-v3 short-duration score, so those decisions
    # must never enter a calibration corpus. v3+ and funnel-lineage (empty version)
    # decisions are kept; pre-v3 short-duration ones are dropped and counted.
    from app.analytics.calibration import eligible_for_calibration

    v2 = _snap("v2a", iv_rank=0.3)
    v2.scoring_model_version = "sd-scoring-2026.07-v2"
    v3 = _snap("v3a", iv_rank=0.3)
    v3.scoring_model_version = "sd-scoring-2026.07-v3"
    funnel = _snap("fun", iv_rank=0.3)  # empty version = funnel lineage, different model
    kept, n_excluded = eligible_for_calibration([v2, v3, funnel])
    assert n_excluded == 1
    assert {s.decision_id for s in kept} == {"v3a", "fun"}

    # build_scorecard applies the filter and surfaces the exclusion count + a warning.
    outs = [_marks_outcome("v3a", 100, 1), _marks_outcome("fun", -60, 2),
            _marks_outcome("v2a", 100, 3)]
    card = build_scorecard([v2, v3, funnel], outs)
    assert card.n_excluded_pre_v3 == 1
    assert card.n_resolved == 2  # only v3a + fun graded; v2a dropped before pairing
    assert any("pre-v3" in w for w in card.warnings)


def test_scorecard_grades_real_marks_and_reports_pnl_and_drawdown() -> None:
    # A mixed, multi-regime book: enough losses + >1 regime -> no degeneracy flag.
    # winners span two regimes (d1 extreme, d3 fair); 2 losses -> no degeneracy flag.
    snaps = [
        _snap("d1", iv_rank=0.8), _snap("d2", iv_rank=0.3), _snap("d3", iv_rank=0.3),
        _snap("d4", iv_rank=0.8),
    ]
    outs = [
        _marks_outcome("d1", 100, 1), _marks_outcome("d2", -60, 2),
        _marks_outcome("d3", 100, 3), _marks_outcome("d4", -80, 4),
    ]
    card = build_scorecard(snaps, outs)
    assert card.validation_grade == "real_marks"  # all decisive are option_marks
    assert card.net_pnl_usd == 60.0  # 100 - 60 + 100 - 80
    assert card.max_drawdown_usd == 80.0  # 200 peak -> 120 -> 60... worst run 80
    assert card.profit_factor == round(200 / 140, 3)
    assert {g.key for g in card.by_vol_regime} == {"fair", "extreme"}  # 0.3->fair, 0.8->extreme
    assert card.warnings == []  # 2 losses across 2 regimes -> clean


def test_scorecard_flags_no_loss_single_regime_book() -> None:
    # The dangerous shape: all wins, one regime -> cannot be validated.
    snaps = [_snap(f"w{i}", iv_rank=0.85) for i in range(4)]
    outs = [_marks_outcome(f"w{i}", 50 + i, i + 1) for i in range(4)]
    card = build_scorecard(snaps, outs)
    assert any("loss" in w for w in card.warnings)
    assert any("single vol regime" in w and "extreme" in w for w in card.warnings)


def test_scorecard_reports_per_horizon_calibration() -> None:
    # Phase 4: one ledger, calibration bucketed by trade horizon (0DTE/1-5DTE/swing).
    snaps = [
        _snap("z", iv_rank=0.5, dte=0), _snap("s", iv_rank=0.5, dte=3),
        _snap("w1", iv_rank=0.5, dte=30), _snap("w2", iv_rank=0.5, dte=45),
    ]
    outs = [
        _marks_outcome("z", 40, 1), _marks_outcome("s", -30, 2),
        _marks_outcome("w1", 120, 3), _marks_outcome("w2", -50, 4),
    ]
    card = build_scorecard(snaps, outs)
    horizons = {g.key: g.n for g in card.by_horizon}
    assert horizons == {"0DTE": 1, "1-5DTE": 1, "swing": 2}


def test_scorecard_grades_proxy_only_when_no_real_marks() -> None:
    # Only the underlying proxy resolved -> not a trustworthy validation source.
    snaps = [_snap("d1", iv_rank=0.5), _snap("d2", iv_rank=0.5), _snap("d3", iv_rank=0.5)]
    outs = [
        DecisionOutcome(decision_id=s.decision_id, symbol="AAA", horizon_label="5d",
                        resolved_at=datetime(2026, 6, 6, tzinfo=UTC), elapsed_days=5,
                        result=OutcomeResult.WIN if i != 1 else OutcomeResult.LOSS,
                        outcome_source="underlying_vs_breakeven")
        for i, s in enumerate(snaps)
    ]
    card = build_scorecard(snaps, outs)
    assert card.validation_grade == "proxy_only"
    assert card.net_pnl_usd is None  # the proxy carries no dollar P&L
