"""Export invariants, so an audit never has to discover them again.

B5 of the remediation directive. Each assertion here corresponds to a defect the
audit of build 7afa098 found by reading the CSV, which is the wrong place to find
them:

  * `spot_price` read 0.0 on all 67 scanner rows while the data dictionary
    claimed the field was available (B1). A required float plus an `or 0.0`
    fallback turned "missing" into a plausible number.
  * Entry and exit price basis must match within a row; mixing a mid entry with a
    mark exit invalidates the P&L.
  * Missing values must use a sentinel, never blank and never zero, so absence
    stays distinguishable from a measurement.
  * Weights must sum to a documented total.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.domain.enums import (
    Direction,
    DTECategory,
    OptionAction,
    OptionType,
    ShortDurationRegime,
    ShortDurationStrategy,
    StrategyType,
)
from app.domain.outcomes import DecisionOutcome, DecisionSnapshot, OutcomeResult
from app.domain.shortduration import ContractRecommendation, ShortDurationCandidate
from app.domain.trades import ContractLeg, RiskPlan, TradePlan

SENTINELS = {"NA_not_implemented", "NA_no_data", "NA_unresolved"}
_EXP = date(2026, 8, 21)
_NOW = datetime(2026, 8, 3, 14, 30, tzinfo=UTC)


def _plan() -> TradePlan:
    return TradePlan(
        symbol="SPY", direction=Direction.BULLISH, strategy=StrategyType.BULL_CALL_SPREAD,
        legs=[
            ContractLeg(symbol="SPY", action=OptionAction.BUY_TO_OPEN,
                        option_type=OptionType.CALL, strike=500.0, expiration=_EXP,
                        quantity=1, entry_price=2.0),
            ContractLeg(symbol="SPY", action=OptionAction.SELL_TO_OPEN,
                        option_type=OptionType.CALL, strike=505.0, expiration=_EXP,
                        quantity=1, entry_price=1.0),
        ],
        net_debit=100.0, contracts=1,
        risk=RiskPlan(max_loss_usd=100.0, account_risk_pct=0.05,
                      profit_target_pct=0.5, stop_loss_pct=0.5, time_stop_dte=2),
    )


def _snapshot(**kw) -> DecisionSnapshot:
    base = {
        "decision_id": "sd:abc", "scan_id": "sd:abc", "symbol": "SPY",
        "direction": Direction.BULLISH, "strategy": StrategyType.BULL_CALL_SPREAD,
        "generated_at": _NOW, "composite_score": 0.71, "entry_spot": 512.34,
        "entry_net_per_share": 1.0, "max_loss_usd": 100.0, "contracts": 1,
        "expiration": _EXP, "dte_at_entry": 18, "trade_plan": _plan(),
        "scoring_model_version": "sd-scoring-2026.07-v3",
    }
    base.update(kw)
    return DecisionSnapshot(**base)


def _outcome(**kw) -> DecisionOutcome:
    base = {
        "decision_id": "sd:abc", "symbol": "SPY", "horizon_label": "managed_exit",
        "resolved_at": _NOW, "elapsed_days": 2, "result": OutcomeResult.WIN,
        "realized_pnl_usd": 50.0, "realized_pnl_gross_usd": 54.0, "costs_usd": 4.0,
        "outcome_source": "managed_policy", "exit_reason": "profit_target",
    }
    base.update(kw)
    return DecisionOutcome(**base)


def _candidate() -> ShortDurationCandidate:
    from app.domain.shortduration import ScoreCard

    return ShortDurationCandidate(
        id="abc", symbol="SPY", dte_category=DTECategory.SHORT_DTE,
        strategy=ShortDurationStrategy.TREND_CONTINUATION, direction=Direction.BULLISH,
        detected_at=_NOW, regime=ShortDurationRegime.RANGE_BOUND, score=0.71,
        confidence=0.71, entry_spot=512.34,
        contract=ContractRecommendation(description="Call Debit Spread x1"),
        scorecard=ScoreCard(
            dte_category=DTECategory.SHORT_DTE, total=71.0, overall_confidence=0.71,
            weights={"daily_trend": 20.0, "catalyst_news": 15.0, "multi_session_flow": 15.0,
                     "market_alignment": 10.0, "volatility": 10.0, "contract_liquidity": 10.0,
                     "technical_entry": 10.0, "risk_reward": 10.0},
        ),
    )


def _row(**kw):
    from scripts.export_signal_audit import _signal_row

    return _signal_row(_snapshot(**kw.pop("snap", {})), _outcome(**kw.pop("out", {})),
                       kw.pop("cand", _candidate()), "abc1234")


# --- B1: spot must never be a silent zero -------------------------------------
def test_a_real_spot_survives_to_the_export() -> None:
    assert _row()["spot_price"] == pytest.approx(512.34)


def test_a_missing_spot_reports_a_sentinel_not_zero() -> None:
    # THE regression. 0.0 is a price; absence is not.
    assert _row(snap={"entry_spot": None})["spot_price"] in SENTINELS


def test_the_snapshot_model_permits_an_absent_spot() -> None:
    # It was a required float, which is what forced callers into `or 0.0`.
    assert _snapshot(entry_spot=None).entry_spot is None


# --- Basis consistency --------------------------------------------------------
def test_entry_and_exit_basis_match_within_a_row() -> None:
    r = _row()
    assert r["entry_price_basis"] == r["exit_price_basis"]


def test_a_live_row_uses_actual_fill_on_both_sides() -> None:
    from app.domain.outcomes import DecisionSource

    r = _row(snap={"source": DecisionSource.LIVE}, out={"outcome_source": "live_close"},
             cand=None)
    assert r["entry_price_basis"] == r["exit_price_basis"] == "actual_fill"


# --- Sentinel discipline ------------------------------------------------------
def test_no_cell_is_ever_blank() -> None:
    assert all(v != "" and v is not None for v in _row().values())


def test_unavailable_fields_use_a_declared_sentinel() -> None:
    r = _row()
    for col in ("option_bid", "mfe", "mae", "gex_proxy", "vrp", "term_slope"):
        assert r[col] in SENTINELS, f"{col} must use a sentinel, got {r[col]!r}"


def test_a_string_field_never_silently_empties() -> None:
    r = _row(snap={"scoring_model_version": ""})
    assert r["scanner_version"] in SENTINELS


# --- Weights ------------------------------------------------------------------
def test_weights_sum_to_the_documented_total() -> None:
    assert _row()["weights_sum"] == pytest.approx(100.0)


def test_a_row_without_a_scorecard_reports_no_weight_total() -> None:
    assert _row(cand=None)["weights_sum"] in SENTINELS


# --- Phase 1: the captured market context reaches the CSV ---------------------
def _mc():
    from app.domain.market_context import GREEKS_MODELED, LegQuote, MarketContext

    return MarketContext(
        legs=[
            LegQuote(strike=500.0, option_type=OptionType.CALL, expiration=_EXP,
                     signed_quantity=1, bid=1.90, ask=2.10, mid=2.0, spread=0.20,
                     spread_pct_of_mid=0.10, volume=1200, open_interest=5400,
                     implied_volatility=0.24, delta=0.55, gamma=0.02, theta=-0.04,
                     vega=0.11, greeks_source=GREEKS_MODELED, quote_source="unusual_whales"),
            LegQuote(strike=505.0, option_type=OptionType.CALL, expiration=_EXP,
                     signed_quantity=-1, bid=0.90, ask=1.10, mid=1.0, spread=0.20,
                     spread_pct_of_mid=0.20, volume=800, open_interest=3100,
                     implied_volatility=0.23, delta=0.40, gamma=0.02, theta=-0.03,
                     vega=0.09, greeks_source=GREEKS_MODELED, quote_source="unusual_whales"),
        ],
        cost_drag_ratio=0.18, round_trip_cost_usd=40.0,
        iv30=0.22, iv_rank=0.45, realized_vol_20d=0.18, vrp_points=0.04,
        term_structure_slope=0.03, implied_move_pct=0.053,
        earnings_days_away=32, signal_build_sha="abc123def456",
        regime_tag="midvol/uptape", regime_vol="midvol", regime_tape="uptape",
    )


def test_the_captured_nbbo_reaches_the_export() -> None:
    # The audit's finding was that this was computed on every scan and discarded.
    r = _row(snap={"market_context": _mc()})
    assert r["option_bid"] == 1.90 and r["option_ask"] == 2.10
    assert r["spread_pct"] == 0.10


def test_flat_quote_columns_describe_the_long_leg() -> None:
    # A spread has two books; the flat columns must name which one they mean.
    r = _row(snap={"market_context": _mc()})
    assert r["option_mid"] == 2.0  # the long 500 call, not the short 505
    assert r["volume"] == 1200 and r["open_interest"] == 5400


def test_every_leg_survives_in_full_alongside_the_flat_columns() -> None:
    # Item 1.1 is NBBO per LEG. Collapsing to one loses the measurement.
    import json

    legs = json.loads(_row(snap={"market_context": _mc()})["legs_nbbo_json"])
    assert len(legs) == 2
    assert {lg["strike"] for lg in legs} == {500.0, 505.0}
    assert legs[1]["qty"] == -1


def test_greeks_are_never_exported_without_their_provenance() -> None:
    r = _row(snap={"market_context": _mc()})
    assert r["greeks_source"] == "black_scholes_modeled"
    assert r["net_delta_modeled"] == pytest.approx(0.15)  # 0.55 - 0.40


def test_structure_greeks_are_signed_by_position() -> None:
    r = _row(snap={"market_context": _mc()})
    assert r["theta_at_entry"] == pytest.approx(-0.04 + 0.03)
    assert r["vega_at_entry"] == pytest.approx(0.11 - 0.09)


def test_the_regime_tag_and_producing_build_reach_the_export() -> None:
    r = _row(snap={"market_context": _mc()})
    assert r["regime_tag"] == "midvol/uptape"
    assert r["regime_tape"] == "uptape"
    # Distinct from export_git_sha, which is the build that made the CSV.
    assert r["signal_build_sha"] == "abc123def456"


def test_cost_drag_and_its_dollar_denominator_both_ship() -> None:
    r = _row(snap={"market_context": _mc()})
    assert r["cost_drag_ratio"] == 0.18 and r["round_trip_cost_usd"] == 40.0


def test_a_row_predating_capture_says_no_data_not_not_implemented() -> None:
    # THE sentinel distinction. These rows exist (145 of them); the capability
    # now exists too. "Not implemented" would misdescribe both.
    r = _row(snap={"market_context": None})
    for col in ("option_bid", "cost_drag_ratio", "regime_tag", "signal_build_sha",
                "legs_nbbo_json", "greeks_source", "net_delta_modeled"):
        assert r[col] == "NA_no_data", f"{col} was {r[col]!r}"


def test_the_new_columns_are_never_blank() -> None:
    for mc in (_mc(), None):
        assert all(v != "" and v is not None for v in _row(snap={"market_context": mc}).values())


# --- Component direction ------------------------------------------------------
def test_component_direction_is_emitted_for_the_inverted_scoring_check() -> None:
    r = _row()
    # Present on every slot, populated or sentinel — the column must exist so the
    # inverted-scoring failure mode stays checkable from the CSV alone.
    for i in range(1, 9):
        assert f"component_{i}_direction" in r
