"""Shadow flow-quality instrumentation.

The sibling scanner's premium-weighted flow-conviction metric is ported and
recorded alongside frozen decisions, but it must NOT change any decision — it
rides a `SignalScore.details` entry the scorer never reads. These tests pin
three things: the metric's arithmetic, its byte-for-byte decision neutrality,
and that the ledger can now grade it (correlation + promotion verdict).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.analytics.calibration import build_scorecard
from app.analytics.metrics import spearman
from app.analytics.snapshots import snapshot_from_candidate
from app.domain.candidates import Thesis, TradeCandidate
from app.domain.enums import CandidateStatus, Direction, StrategyType
from app.domain.options import FlowAlert, OptionType
from app.domain.outcomes import (
    DecisionOutcome,
    DecisionSnapshot,
    DecisionSource,
    OutcomeResult,
)
from app.domain.signals import SignalBundle, SignalScore
from app.domain.trades import RiskPlan, TradePlan
from app.engine.flow import analyze_flow
from app.engine.flow_quality import proprietary_flow_quality
from app.engine.scoring import composite_score

_TS = datetime(2026, 6, 1, tzinfo=UTC)


def _alert(**kw) -> FlowAlert:
    base = {
        "symbol": "AAA", "option_type": OptionType.CALL, "strike": 100.0,
        "expiration": date(2026, 7, 17), "premium": 100_000.0, "size": 500,
        "open_interest": 1000, "is_sweep": False, "is_opening": None,
        "sentiment": 0.5, "ts": _TS,
    }
    base.update(kw)
    return FlowAlert(**base)


# --- Step 1: the metric's arithmetic ----------------------------------------


def test_bare_print_scores_the_base() -> None:
    # No conviction markers, size/OI = 0.5 -> only the medium vol/OI bump.
    a = _alert(is_opening=False, is_sweep=False, size=500, open_interest=1000)
    assert proprietary_flow_quality([a]) == 0.55  # 0.5 base + 0.05 (voi 0.5)


def test_all_markers_stack_and_clamp() -> None:
    # opening + sweep + repeated + voi>=1.0 -> 0.5+.20+.10+.15+.10 = 1.05 -> 1.0.
    a1 = _alert(is_opening=True, is_sweep=True, size=2000, open_interest=1000)
    a2 = _alert(is_opening=True, is_sweep=True, size=2000, open_interest=1000)
    # a1/a2 share (type, strike, expiry) -> both count as repeated.
    assert proprietary_flow_quality([a1, a2]) == 1.0


def test_premium_weighting_favors_the_big_print() -> None:
    strong = _alert(strike=100.0, premium=900_000.0, is_opening=True, is_sweep=True,
                    size=2000, open_interest=1000)  # quality 1.0-ish, huge weight
    weak = _alert(strike=105.0, premium=10_000.0, is_opening=False, is_sweep=False,
                  size=100, open_interest=10_000)  # quality 0.5, tiny weight
    q = proprietary_flow_quality([strong, weak])
    # Strong print quality is 0.90 (opening+sweep+voi), weak is 0.50. The
    # premium-weighted mean sits near the strong print, far above the 0.70 midpoint.
    assert q is not None and q > 0.88


def test_no_premium_falls_back_to_equal_weight_mean() -> None:
    a1 = _alert(premium=None, is_opening=True, is_sweep=False, size=100, open_interest=10_000)
    a2 = _alert(strike=105.0, premium=None, is_opening=False, is_sweep=False,
                size=100, open_interest=10_000)
    # qualities: 0.70 (opening) and 0.50 -> equal-weight mean 0.60.
    assert proprietary_flow_quality([a1, a2]) == 0.6


def test_empty_flow_is_none() -> None:
    assert proprietary_flow_quality([]) is None


# --- Step 2: decision neutrality --------------------------------------------


def test_metric_rides_flow_signal_details() -> None:
    sig = analyze_flow("AAA", [_alert(is_opening=True, is_sweep=True)])
    assert "flow_quality_proprietary" in sig.details
    assert isinstance(sig.details["flow_quality_proprietary"], float)


def test_no_flow_records_none_not_a_crash() -> None:
    sig = analyze_flow("AAA", [])
    assert sig.details["flow_quality_proprietary"] is None


def test_shadow_detail_never_moves_the_composite_score() -> None:
    flow = SignalScore(name="options_flow", score=0.8, direction=Direction.BULLISH,
                       details={"flow_quality_proprietary": 0.95})
    price = SignalScore(name="price_action", score=0.6, direction=Direction.BULLISH)
    before = composite_score(SignalBundle(symbol="AAA", scores=[flow, price]))
    flow.details["flow_quality_proprietary"] = 0.10  # mutate the shadow value
    after = composite_score(SignalBundle(symbol="AAA", scores=[flow, price]))
    assert before == after  # the scorer reads .score, never .details


# --- Step 2b: snapshot carries it -------------------------------------------


def _candidate_with_flow_quality(q: float) -> TradeCandidate:
    flow = SignalScore(name="options_flow", score=0.7, direction=Direction.BULLISH,
                       details={"flow_quality_proprietary": q})
    plan = TradePlan(
        symbol="AAA", direction=Direction.BULLISH, strategy=StrategyType.BULL_PUT_SPREAD,
        legs=[], net_debit=-100.0, contracts=1,
        risk=RiskPlan(max_loss_usd=400.0, max_profit_usd=100.0, account_risk_pct=0.05,
                      profit_target_pct=0.5, stop_loss_pct=0.5),
    )
    thesis = Thesis(direction=Direction.BULLISH, why_now="flow", flow_meaningful=True,
                    price_confirms=True, has_catalyst=False, iv_favorable=True,
                    invalidation="n/a")
    return TradeCandidate(
        symbol="AAA", status=CandidateStatus.RANKED, composite_score=0.7,
        direction=Direction.BULLISH, thesis=thesis, signals=[flow], trade_plan=plan,
        generated_at=_TS, scan_id="scan1",
    )


def test_snapshot_freezes_the_shadow_metric() -> None:
    snap = snapshot_from_candidate(_candidate_with_flow_quality(0.82))
    assert snap is not None
    assert snap.flow_quality_proprietary == 0.82
    # And it round-trips through JSON persistence (payload) with no migration.
    assert DecisionSnapshot.model_validate(
        snap.model_dump(mode="json")
    ).flow_quality_proprietary == 0.82


# --- Step 3: the ledger can grade it ----------------------------------------


def test_spearman_matches_a_known_value() -> None:
    # Perfectly monotone -> +1; reversed -> -1.
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == -1.0
    assert spearman([1, 2], [3, 4]) is None  # too few points


def _snap(did: str, *, flow_q: float | None, score: float) -> DecisionSnapshot:
    plan = TradePlan(
        symbol="AAA", direction=Direction.BULLISH, strategy=StrategyType.BULL_PUT_SPREAD,
        legs=[], net_debit=-100.0, contracts=1,
        risk=RiskPlan(max_loss_usd=400.0, max_profit_usd=100.0, account_risk_pct=0.05,
                      profit_target_pct=0.5, stop_loss_pct=0.5),
    )
    return DecisionSnapshot(
        decision_id=did, scan_id="s", symbol="AAA", source=DecisionSource.SCAN,
        direction=Direction.BULLISH, strategy=StrategyType.BULL_PUT_SPREAD,
        generated_at=_TS, composite_score=score, probability_of_profit=0.6,
        iv_rank=0.5, flow_quality_proprietary=flow_q, entry_spot=100.0,
        entry_net_per_share=-1.0, max_loss_usd=400.0, max_profit_usd=100.0,
        contracts=1, expiration=date(2026, 7, 17), dte_at_entry=46, trade_plan=plan,
    )


def _out(did: str, pnl: float, day: int) -> DecisionOutcome:
    result = OutcomeResult.WIN if pnl > 0 else OutcomeResult.LOSS
    return DecisionOutcome(
        decision_id=did, symbol="AAA", horizon_label=f"{day}d",
        resolved_at=datetime(2026, 6, 1 + day, tzinfo=UTC), elapsed_days=day,
        result=result, realized_pnl_usd=pnl, outcome_source="option_marks",
    )


def test_scorecard_correlates_flow_quality_with_pnl_and_bands_it() -> None:
    # Flow quality tracks P&L (higher quality -> better outcome); the composite
    # score is deliberately anti-correlated, so the shadow metric shows lift.
    rows = [
        ("a", 0.55, 0.9, -80), ("b", 0.62, 0.8, -30), ("c", 0.70, 0.7, 40),
        ("d", 0.78, 0.6, 90), ("e", 0.85, 0.5, 150),
    ]
    snaps = [_snap(d, flow_q=q, score=sc) for d, q, sc, _ in rows]
    outs = [_out(d, pnl, i + 1) for i, (d, _, _, pnl) in enumerate(rows)]
    card = build_scorecard(snaps, outs)

    assert card.flow_quality_pnl_spearman == 1.0  # monotone with P&L
    assert card.score_pnl_spearman == -1.0
    assert card.flow_quality_lift == 2.0  # 1.0 - (-1.0)
    bands = {g.key for g in card.by_flow_quality_band}
    assert bands == {"weak", "moderate", "strong"}


def test_flow_quality_verdict_needs_a_real_sample() -> None:
    # Five points, positive correlation -> still 'insufficient' (n < 10).
    rows = [("a", 0.55, -50), ("b", 0.62, -20), ("c", 0.70, 30),
            ("d", 0.78, 60), ("e", 0.85, 100)]
    snaps = [_snap(d, flow_q=q, score=0.7) for d, q, _ in rows]
    outs = [_out(d, pnl, i + 1) for i, (d, _, pnl) in enumerate(rows)]
    assert build_scorecard(snaps, outs).flow_quality_verdict == "insufficient"
