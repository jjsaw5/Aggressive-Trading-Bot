"""POP calibration feed: SD decisions → warehouse, per-source POP buckets."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.analytics.calibration import build_scorecard
from app.analytics.snapshots import SD_POP_SOURCE, snapshot_from_short_duration
from app.config import get_settings
from app.domain.enums import (
    CandidateState,
    Direction,
    DTECategory,
    OptionAction,
    OptionType,
    StrategyType,
)
from app.domain.outcomes import DecisionOutcome, OutcomeResult
from app.domain.shortduration import ScoreCard, ShortDurationCandidate
from app.domain.trades import ContractLeg, RiskPlan, TradePlan

_NOW = datetime(2026, 7, 20, 15, 0, tzinfo=UTC)


def _plan() -> TradePlan:
    leg = ContractLeg(symbol="SPY", action=OptionAction.BUY_TO_OPEN,
                      option_type=OptionType.CALL, strike=101.0,
                      expiration=date(2026, 7, 24), quantity=1, entry_price=1.2)
    return TradePlan(
        symbol="SPY", direction=Direction.BULLISH, strategy=StrategyType.LONG_CALL,
        legs=[leg], net_debit=120.0, contracts=1,
        risk=RiskPlan(max_loss_usd=120.0, max_profit_usd=None, account_risk_pct=0.06,
                      reward_to_risk=2.0, profit_target_pct=0.5, stop_loss_pct=0.5),
    )


def _cand(*, pop: float | None = 0.42, abstained: bool = False,
          plan: TradePlan | None = None) -> ShortDurationCandidate:
    card = ScoreCard(dte_category="1-5dte", total=60.0, overall_confidence=0.5,
                     factors=[], components={}, data_quality=0.9, summary="s",
                     abstained=abstained, abstain_reason="low coverage" if abstained else "")
    return ShortDurationCandidate(
        id="abc123", symbol="SPY", dte_category=DTECategory.SHORT_DTE,
        direction=Direction.BULLISH, detected_at=_NOW, state=CandidateState.WATCHLIST,
        score=0.6, probability_of_profit=pop, trade_plan=plan if plan is not None else _plan(),
        scorecard=card,
    )


def test_sd_snapshot_carries_pop_source_and_version() -> None:
    snap = snapshot_from_short_duration(_cand())
    assert snap is not None
    assert snap.decision_id == "sd:abc123"
    assert snap.probability_of_profit == 0.42
    assert snap.pop_source == SD_POP_SOURCE
    assert snap.scoring_model_version == get_settings().scoring_model_version  # v3+
    assert snap.breakevens == [102.2]  # strike + net/100
    assert snap.dte_at_entry == 4


def test_sd_snapshot_skips_abstained_and_planless() -> None:
    # Abstained rank = insufficient inputs -> not a decision; no plan -> nothing to grade.
    assert snapshot_from_short_duration(_cand(abstained=True)) is None
    c = _cand()
    c.trade_plan = None
    assert snapshot_from_short_duration(c) is None


def test_sd_snapshot_without_pop_has_empty_pop_source() -> None:
    snap = snapshot_from_short_duration(_cand(pop=None))
    assert snap is not None
    assert snap.probability_of_profit is None
    assert snap.pop_source == ""  # never label a construct that produced nothing


def _outcome(did: str, win: bool, day: int) -> DecisionOutcome:
    return DecisionOutcome(
        decision_id=did, symbol="SPY", horizon_label=f"{day}d",
        resolved_at=datetime(2026, 7, 20 + day, tzinfo=UTC), elapsed_days=day,
        result=OutcomeResult.WIN if win else OutcomeResult.LOSS,
        realized_pnl_usd=50.0 if win else -60.0, outcome_source="option_marks",
    )


def test_scorecard_buckets_pop_per_source() -> None:
    # Two constructs must never pool: the SD traded-expiry POP and the legacy
    # funnel POP get separate calibration buckets.
    sd = snapshot_from_short_duration(_cand())
    legacy = snapshot_from_short_duration(_cand())
    assert sd is not None and legacy is not None
    legacy = legacy.model_copy(update={
        "decision_id": "fun:1", "pop_source": "", "probability_of_profit": 0.62,
        "scoring_model_version": "",
    })
    outs = [_outcome("sd:abc123", True, 2), _outcome("fun:1", False, 3)]
    card = build_scorecard([sd, legacy], outs)
    assert set(card.pop_buckets_by_source) == {SD_POP_SOURCE, "funnel_analytics"}
    sd_buckets = card.pop_buckets_by_source[SD_POP_SOURCE]
    assert sum(b.n for b in sd_buckets) == 1
    # The SD 0.42 falls in the 30-50% bucket and resolved as a win.
    b = next(b for b in sd_buckets if b.label == "30%-50%")
    assert b.realized_win_rate == 1.0 and b.avg_predicted_pop == 0.42