"""Short-duration Phase 3 — scoring models, news, flow decay, state machine."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.domain.enums import CandidateState, Direction, DTECategory, OptionType, ShortDurationRegime
from app.domain.market import Candle, PriceHistory
from app.domain.options import FlowAlert, IVContext, OptionChain, OptionContract
from app.domain.shortduration import (
    IntradayLevels,
    NewsItem,
    ShortDurationCandidate,
    ShortDurationRegimeState,
)
from app.shortduration.scoring.data_quality import compute_data_quality
from app.shortduration.scoring.engine import score_candidate
from app.shortduration.scoring.flow_decay import DecayConfig, analyze_flow, decay_weight
from app.shortduration.scoring.news import (
    DedupState,
    best_news_score,
    classify_direction,
    score_news,
)
from app.shortduration.state import can_transition, classify_initial_state, transition
from app.shortduration.strategies.base import SetupContext, StrategyDetection

_NOW = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)


def _regime(**kw):
    base = {"regime": ShortDurationRegime.RANGE_BOUND, "confidence": 0.5,
            "allow_new_trades": True, "breadth_above_vwap_pct": 0.5, "as_of": _NOW}
    base.update(kw)
    return ShortDurationRegimeState(**base)


def _levels(**kw):
    base = {"symbol": "SPY", "session_date": date(2026, 7, 17), "last": 101.0, "vwap": 100.0,
            "opening_range_high": 100.5, "opening_range_low": 99.0, "relative_volume": 2.0,
            "computed_at": _NOW}
    base.update(kw)
    return IntradayLevels(**base)


def _detection(direction=Direction.BULLISH, dte=DTECategory.ZERO_DTE):
    from app.domain.enums import ShortDurationStrategy

    return StrategyDetection(
        strategy=ShortDurationStrategy.OPENING_RANGE_BREAKOUT, dte_category=dte,
        direction=direction, setup_score=0.6, entry_trigger="e", invalidation="i",
    )


def _chain() -> OptionChain:
    return OptionChain(
        symbol="SPY", underlying_price=101.0, as_of=_NOW, source="test",
        contracts=[
            OptionContract(
                symbol="SPY", expiration=date(2026, 7, 20), strike=101.0,
                option_type=OptionType.CALL,
                bid=1.0, ask=1.05, open_interest=5000, volume=1200, implied_volatility=0.3,
                as_of=_NOW, source="test",
            )
        ],
    )


def _iv(rank=0.35) -> IVContext:
    return IVContext(symbol="SPY", iv30=0.3, iv_rank=rank, as_of=_NOW, source="test")


# --- Flow decay --------------------------------------------------------------
def test_decay_weight_buckets() -> None:
    cfg = DecayConfig()
    assert decay_weight(60, cfg) == 1.0
    assert decay_weight(200, cfg) == 0.8
    assert decay_weight(600, cfg) == 0.5
    assert decay_weight(2000, cfg) == cfg.context_weight


def test_analyze_flow_detects_opposing() -> None:
    flow = [
        FlowAlert(symbol="X", sentiment=0.7, ts=_NOW - timedelta(seconds=30), strike=100.0),
        FlowAlert(symbol="X", sentiment=-0.6, ts=_NOW - timedelta(seconds=60), strike=100.0),
    ]
    fa = analyze_flow(flow, _NOW, Direction.BULLISH)
    assert fa.opposing_present is True
    assert fa.repeated_strikes is True
    assert fa.prints == 2


def test_analyze_flow_weights_recent_more() -> None:
    recent = [FlowAlert(symbol="X", sentiment=0.9, ts=_NOW - timedelta(seconds=30))]
    stale = [FlowAlert(symbol="X", sentiment=0.9, ts=_NOW - timedelta(minutes=45))]
    assert analyze_flow(recent, _NOW).confidence > analyze_flow(stale, _NOW).confidence


# --- News scoring ------------------------------------------------------------
def test_news_direction_classifier() -> None:
    assert classify_direction("Apple upgraded to Buy, PT raised") == Direction.BULLISH
    assert classify_direction("Stock plunges on SEC probe and lawsuit") == Direction.BEARISH
    assert classify_direction("Company holds annual meeting") == Direction.NEUTRAL


def test_news_score_weights_and_dedup() -> None:
    item = NewsItem(id="1", symbol="AAPL", headline="AAPL upgraded, guidance raised",
                    source="Benzinga", received_ts=_NOW, source_ts=_NOW - timedelta(minutes=1))
    sc = score_news(item, for_symbol="AAPL", change_pct=1.5, rel_volume=2.0)
    assert 0.0 <= sc.total <= 1.0
    assert sc.source_authority >= 0.9  # Benzinga is a high-authority source
    assert sc.price_confirmed > 0  # price move aligns with a bullish headline
    dd = DedupState()
    dd.add(item.headline)
    dup = score_news(item, dedup=dd)
    assert dup.is_duplicate is True and dup.novelty < 0.2


def test_best_news_score_picks_most_material() -> None:
    items = [
        NewsItem(id="a", symbol="AAPL", headline="AAPL misc note", source="blog", received_ts=_NOW),
        NewsItem(id="b", symbol="AAPL", headline="AAPL wins FDA approval, upgraded",
                 source="Reuters", received_ts=_NOW, source_ts=_NOW),
    ]
    best = best_news_score(items, for_symbol="AAPL")
    assert best is not None and best.total > 0.5


# --- Data quality ------------------------------------------------------------
def test_data_quality_flags_missing() -> None:
    ctx = SetupContext(symbol="SPY", now=_NOW, regime=_regime(), levels=_levels())
    dq = compute_data_quality(ctx, chain=None, iv=None, dte=DTECategory.ZERO_DTE)
    assert dq.value is not None and dq.value < 1.0
    assert "missing" in dq.explanation


# --- Scoring engine ----------------------------------------------------------
def test_score_candidate_0dte_weights_sum_to_100() -> None:
    ctx = SetupContext(symbol="SPY", now=_NOW, regime=_regime(), levels=_levels(),
                       change_pct=0.8, quote=None)
    card = score_candidate(ctx, _detection(), chain=_chain(), iv=_iv())
    assert card.dte_category == "0dte"
    assert sum(f.weight for f in card.factors) == 100
    assert 0 <= card.total <= 100
    assert card.overall_confidence <= card.normalized  # tempered by data quality
    assert "data_quality" in card.components and "liquidity" in card.components


def test_score_candidate_missing_input_is_low_not_neutral() -> None:
    # No chain/iv -> liquidity & volatility factors are flagged low, not 0.5.
    ctx = SetupContext(symbol="SPY", now=_NOW, regime=_regime(), levels=_levels())
    card = score_candidate(ctx, _detection(), chain=None, iv=None)
    liq = next(f for f in card.factors if f.key == "contract_liquidity")
    assert liq.raw < 0.5 and "no data" in liq.explanation.lower()


def test_conviction_stamped_uncalibrated_and_flags_missing_pop() -> None:
    # Honest degrade: the hand-weighted score is a TRADABILITY rank, never displayed
    # as earned conviction until a calibration gate is green.
    ctx = SetupContext(symbol="SPY", now=_NOW, regime=_regime(), levels=_levels(), change_pct=0.8)
    card = score_candidate(ctx, _detection(), chain=_chain(), iv=_iv())
    assert card.conviction_status == "UNCALIBRATED"
    assert card.pop_available is True
    assert "UNCALIBRATED" in card.summary and "tradability" in card.summary.lower()
    assert "not calibrated conviction" in card.summary.lower()
    # No IV -> POP uncomputable: must not read as high conviction (the blank-POP case).
    blind = score_candidate(ctx, _detection(), chain=_chain(), iv=None)
    assert blind.pop_available is False
    assert "POP unknown" in blind.summary
    assert "pop uncomputable" in blind.conviction_note.lower()


def test_0dte_v2_weights_are_configured_and_versioned() -> None:
    # v2 rebalance: more on structure + liquidity, less on raw flow.
    from app.config import get_settings

    s = get_settings()
    w = s.scoring_0dte_weights
    assert sum(w.values()) == 100
    assert w["price_structure"] == 22 and w["contract_liquidity"] == 18
    assert w["flow_quality"] == 10  # trimmed from the v1 15
    ctx = SetupContext(symbol="SPY", now=_NOW, regime=_regime(), levels=_levels(),
                       change_pct=0.8, quote=None)
    card = score_candidate(ctx, _detection(), chain=_chain(), iv=_iv())
    # Scorecard records exactly what it was scored under.
    assert card.model_version == s.scoring_model_version
    assert card.risk_policy_version == s.risk_policy_version
    assert card.weights == {f.key: f.weight for f in card.factors}
    price = next(f for f in card.factors if f.key == "price_structure")
    liq = next(f for f in card.factors if f.key == "contract_liquidity")
    assert price.weight == 22 and liq.weight == 18


def test_1_5dte_weights_unchanged_and_sum_100() -> None:
    from app.config import get_settings

    w = get_settings().scoring_1_5dte_weights
    assert sum(w.values()) == 100 and w["daily_trend"] == 20 and w["catalyst_news"] == 15


def test_score_keeps_risk_execution_liquidity_freshness_separate() -> None:
    # A composite total must not paper over a bad spread: liquidity / execution /
    # risk / data-quality stay individually inspectable on the scorecard.
    ctx = SetupContext(symbol="SPY", now=_NOW, regime=_regime(), levels=_levels(),
                       change_pct=0.8)
    card = score_candidate(ctx, _detection(), chain=_chain(), iv=_iv())
    for key in ("risk_quality", "execution_quality", "liquidity", "data_quality"):
        assert key in card.components


def test_candidate_records_scoring_versions() -> None:
    from types import SimpleNamespace

    from app.config import get_settings
    from app.domain.shortduration import ContractRecommendation
    from app.shortduration.detection import _candidate_from

    s = get_settings()
    ctx = SetupContext(symbol="SPY", now=_NOW, regime=_regime(), levels=_levels(), change_pct=0.8)
    card = score_candidate(ctx, _detection(), chain=_chain(), iv=_iv())
    contract = SimpleNamespace(
        recommendation=ContractRecommendation(description=""), plan=None, reject_reasons=[]
    )
    gate = SimpleNamespace(allowed=True, reasons=[], reject_reasons=[])
    cand = _candidate_from(_detection(), "SPY", _NOW, card, None, _regime(), contract, gate)
    assert cand.scoring_model_version == s.scoring_model_version
    assert cand.risk_policy_version == s.risk_policy_version


def test_score_candidate_1_5dte_model() -> None:
    closes = [100 + i * 0.6 for i in range(60)]
    daily = PriceHistory(
        symbol="AAPL",
        candles=[Candle(ts=_NOW - timedelta(days=60 - i), open=c, high=c + 1, low=c - 1,
                        close=c, volume=1_000_000) for i, c in enumerate(closes)],
        source="test",
    )
    ctx = SetupContext(symbol="AAPL", now=_NOW, regime=_regime(), levels=_levels(),
                       daily=daily, change_pct=0.5)
    card = score_candidate(ctx, _detection(dte=DTECategory.SHORT_DTE), chain=_chain(), iv=_iv())
    assert card.dte_category == "1-5dte"
    assert sum(f.weight for f in card.factors) == 100
    assert any(f.key == "daily_trend" and f.raw > 0.5 for f in card.factors)


# --- State machine -----------------------------------------------------------
def test_state_machine_legal_and_illegal() -> None:
    assert can_transition(CandidateState.DETECTED, CandidateState.EVALUATING) is True
    assert can_transition(CandidateState.DETECTED, CandidateState.OPEN) is False
    assert can_transition(CandidateState.CLOSED, CandidateState.OPEN) is False  # terminal
    assert can_transition(CandidateState.ARMED, CandidateState.REJECTED) is True  # reject anytime


def test_transition_records_and_rejects_illegal() -> None:
    c = ShortDurationCandidate(id="x", symbol="SPY", dte_category=DTECategory.ZERO_DTE,
                               detected_at=_NOW, state=CandidateState.DETECTED, score=0.8)
    tr = transition(c, CandidateState.EVALUATING, trigger="scored", reason="r", at=_NOW)
    assert c.state == CandidateState.EVALUATING
    assert tr.from_state == CandidateState.DETECTED and tr.to_state == CandidateState.EVALUATING
    assert tr.score_at == 0.8
    with pytest.raises(ValueError):
        transition(c, CandidateState.OPEN, trigger="x")  # illegal jump


def test_classify_initial_state_thresholds() -> None:
    assert classify_initial_state(0.8, watchlist_at=0.5, arm_at=0.7) == CandidateState.ARMED
    assert classify_initial_state(0.6, watchlist_at=0.5, arm_at=0.7) == CandidateState.WATCHLIST
    assert classify_initial_state(0.3, watchlist_at=0.5, arm_at=0.7) == CandidateState.EVALUATING


def test_classify_initial_state_allow_arm_false_caps_at_watchlist() -> None:
    # Layer-1 arming discipline: an arm-worthy score is held at WATCHLIST when arming
    # is disallowed (POP uncomputable, or 0DTE conviction off). Below-arm behaviour
    # is unchanged — a merely-watchable score still watches.
    assert classify_initial_state(
        0.8, watchlist_at=0.5, arm_at=0.7, allow_arm=False
    ) == CandidateState.WATCHLIST
    assert classify_initial_state(
        0.6, watchlist_at=0.5, arm_at=0.7, allow_arm=False
    ) == CandidateState.WATCHLIST
    assert classify_initial_state(
        0.3, watchlist_at=0.5, arm_at=0.7, allow_arm=False
    ) == CandidateState.EVALUATING


def test_classify_transitions_withholds_arm_without_pop() -> None:
    # An arm-worthy score with no computable POP must NOT arm — the TSLA-put-spread
    # failure mode. It is held at WATCHLIST with the reason recorded on the candidate
    # and in the transition trail.
    from app.domain.shortduration import ScoreCard
    from app.shortduration.detection import _classify_transitions

    card = ScoreCard(dte_category="1-5dte", total=80.0, overall_confidence=0.8,
                     factors=[], components={}, data_quality=0.8, summary="s",
                     pop_available=False, conviction_status="UNCALIBRATED")
    cand = ShortDurationCandidate(
        id="y", symbol="TSLA", dte_category=DTECategory.SHORT_DTE, detected_at=_NOW,
        state=CandidateState.DETECTED, score=0.8, scorecard=card,
    )
    trail = _classify_transitions(cand, _detection(dte=DTECategory.SHORT_DTE), _NOW, tradeable=True)
    assert cand.state == CandidateState.WATCHLIST  # capped, not armed
    assert any("POP uncomputable" in r for r in cand.reasons)
    assert trail[-1].to_state == CandidateState.WATCHLIST


def test_classify_transitions_withholds_arm_for_0dte_by_default() -> None:
    # 0DTE asserts no calibrated conviction (ZERO_DTE_CONVICTION=false) — even a fully
    # POP-computable, arm-worthy 0DTE score is held at WATCHLIST.
    from app.domain.shortduration import ScoreCard
    from app.shortduration.detection import _classify_transitions

    card = ScoreCard(dte_category="0dte", total=80.0, overall_confidence=0.8,
                     factors=[], components={}, data_quality=0.8, summary="s",
                     pop_available=True, conviction_status="UNCALIBRATED")
    cand = ShortDurationCandidate(
        id="z", symbol="SPY", dte_category=DTECategory.ZERO_DTE, detected_at=_NOW,
        state=CandidateState.DETECTED, score=0.8, scorecard=card,
    )
    trail = _classify_transitions(cand, _detection(dte=DTECategory.ZERO_DTE), _NOW, tradeable=True)
    assert cand.state == CandidateState.WATCHLIST
    assert any("0DTE" in r for r in cand.reasons)
    assert trail[-1].to_state == CandidateState.WATCHLIST


def test_cost_drag_ratio_from_structure_spread() -> None:
    # Cost-drag = round-trip spread tax / defined max-loss. One leg, $0.10 spread on a
    # $1.00 mid, 1 contract, $100 max-loss -> one-way half-spread $0.05/sh -> round-trip
    # $0.10/sh x 100 = $10 = 10% of $100 max risk.
    from app.domain.enums import OptionAction, OptionType, StrategyType
    from app.domain.shortduration import ContractRecommendation
    from app.domain.trades import ContractLeg, RiskPlan, TradePlan
    from app.shortduration.contracts import ContractResult
    from app.shortduration.detection import _apply_cost_drag

    leg = ContractLeg(symbol="SPY", action=OptionAction.BUY_TO_OPEN,
                      option_type=OptionType.CALL, strike=101.0,
                      expiration=date(2026, 7, 20), quantity=1, entry_price=1.0)
    plan = TradePlan(
        symbol="SPY", direction=Direction.BULLISH, strategy=StrategyType.LONG_CALL,
        legs=[leg], net_debit=100.0, contracts=1,
        risk=RiskPlan(max_loss_usd=100.0, account_risk_pct=0.05,
                      profit_target_pct=0.5, stop_loss_pct=0.5),
    )
    chain = OptionChain(
        symbol="SPY", underlying_price=101.0, as_of=_NOW, source="test",
        contracts=[OptionContract(symbol="SPY", expiration=date(2026, 7, 20), strike=101.0,
                                  option_type=OptionType.CALL, bid=0.95, ask=1.05,
                                  as_of=_NOW, source="test")],
    )
    cr = ContractResult(plan, ContractRecommendation(description="x"))
    _apply_cost_drag(cr, chain, _NOW.date())
    assert cr.recommendation.cost_drag_ratio == pytest.approx(0.10, abs=1e-6)
    assert "max risk" in cr.recommendation.cost_drag_note


# --- Integration: scored detection persists a scorecard + transition trail ---
async def test_run_detection_attaches_scorecard_and_states() -> None:
    from app.shortduration.detection import run_detection

    cands = await run_detection(DTECategory.SHORT_DTE, now=_NOW)
    assert cands
    top = cands[0]
    assert top.scorecard is not None and top.scorecard.total > 0
    assert sum(f.weight for f in top.scorecard.factors) == 100
    # State was driven past DETECTED by the score.
    assert top.state in {CandidateState.EVALUATING, CandidateState.WATCHLIST, CandidateState.ARMED}
