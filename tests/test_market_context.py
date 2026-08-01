"""What the frozen market context must guarantee to be worth capturing.

Phase 1 of the remediation directive. Each assertion corresponds to a way the
audited corpus was unanalysable, or to a way this capture could quietly repeat
the same mistake:

  * B1 was a required float plus an `or 0.0` fallback. Every absent field here
    must stay absent — a plausible number is worse than a gap, because a gap is
    visible in the export and a wrong number is not.
  * No provider in the stack supplies Greeks. Ours are Black-Scholes, and an
    unlabeled modeled Greek would read as vendor data forever.
  * A partial sum over legs looks exactly like a complete measurement. It must
    not be emitted.
  * The pre-registration requires ">=2 distinct regime tags"; three mutually
    incompatible regime classifiers already existed. A vol-only tag cannot
    separate "bad model" from "bad week", which is the audit's core ambiguity.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.analytics.market_context import (
    build_market_context,
    implied_move,
    regime_tag,
    tape_regime,
    vol_regime,
)
from app.domain.enums import OptionAction, OptionType
from app.domain.market_context import GREEKS_MODELED
from app.domain.options import Greeks, OptionChain, OptionContract
from app.domain.trades import ContractLeg, RiskPlan, TradePlan

_EXP = date(2026, 8, 21)
_NOW = datetime(2026, 8, 3, 14, 30, tzinfo=UTC)


def _plan(qty: int = 1) -> TradePlan:
    from app.domain.enums import Direction, StrategyType

    return TradePlan(
        symbol="SPY", direction=Direction.BULLISH, strategy=StrategyType.BULL_CALL_SPREAD,
        legs=[
            ContractLeg(symbol="SPY", action=OptionAction.BUY_TO_OPEN,
                        option_type=OptionType.CALL, strike=500.0, expiration=_EXP,
                        quantity=qty, entry_price=2.0),
            ContractLeg(symbol="SPY", action=OptionAction.SELL_TO_OPEN,
                        option_type=OptionType.CALL, strike=505.0, expiration=_EXP,
                        quantity=qty, entry_price=1.0),
        ],
        net_debit=100.0, contracts=1,
        risk=RiskPlan(max_loss_usd=100.0, account_risk_pct=0.05,
                      profit_target_pct=0.5, stop_loss_pct=0.5),
    )


def _contract(strike: float, *, bid=1.90, ask=2.10, vol=1200, oi=5400, iv=0.24):
    return OptionContract(
        symbol="SPY", option_symbol=f"SPY{strike}", expiration=_EXP, strike=strike,
        option_type=OptionType.CALL, bid=bid, ask=ask, mark=None, last=None,
        volume=vol, open_interest=oi, implied_volatility=iv, greeks=Greeks(),
        as_of=_NOW, source="unusual_whales",
    )


def _chain(*contracts) -> OptionChain:
    return OptionChain(symbol="SPY", underlying_price=502.0,
                       contracts=list(contracts) or [_contract(500.0), _contract(505.0)],
                       as_of=_NOW, source="unusual_whales")


class _IV:
    iv30 = 0.22
    iv_rank = 0.45
    iv_percentile = 0.51
    iv_rank_source = "iv_history"
    term_structure_slope = 0.03
    iv_skew = 0.015
    hv20 = 0.18


def _build(**kw):
    base = {
        "plan": _plan(), "chain": _chain(), "iv_context": _IV(), "spot": 502.0,
        "now": _NOW, "next_earnings": date(2026, 9, 4),
        "daily_closes": [100.0 + i for i in range(30)],
        "cost_drag_ratio": 0.18, "traded_expiry_iv": 0.24,
        "scoring_model_version": "sd-scoring-2026.07-v3",
    }
    base.update(kw)
    return build_market_context(**base)


# --- 1.1 NBBO, per leg -------------------------------------------------------
def test_every_leg_gets_its_own_quote() -> None:
    mc = _build()
    assert len(mc.legs) == 2
    assert all(lg.bid == 1.90 and lg.ask == 2.10 for lg in mc.legs)


def test_mid_and_spread_are_derived_from_the_real_book() -> None:
    lg = _build().legs[0]
    assert lg.mid == pytest.approx(2.00)
    assert lg.spread == pytest.approx(0.20)
    assert lg.spread_pct_of_mid == pytest.approx(0.10)


def test_a_crossed_book_yields_no_mid() -> None:
    # ask < bid is garbage, not a tight market. It must not become a midpoint.
    mc = _build(chain=_chain(_contract(500.0, bid=2.10, ask=1.90), _contract(505.0)))
    assert mc.legs[0].mid is None and mc.legs[0].two_sided is False


def test_a_one_sided_book_yields_no_mid() -> None:
    mc = _build(chain=_chain(_contract(500.0, ask=None), _contract(505.0)))
    assert mc.legs[0].mid is None


def test_a_leg_absent_from_the_chain_reports_nothing_rather_than_zero() -> None:
    # THE B1 failure mode, generalised: a missing quote must not become 0.0.
    mc = _build(chain=_chain(_contract(505.0)))
    missing = next(lg for lg in mc.legs if lg.strike == 500.0)
    assert missing.bid is None and missing.ask is None and missing.mid is None


def test_signed_quantity_records_direction_not_just_size() -> None:
    mc = _build()
    assert [lg.signed_quantity for lg in mc.legs] == [1, -1]


# --- 1.2 cost drag -----------------------------------------------------------
def test_cost_drag_survives_to_the_context() -> None:
    # It was computed on every scan and dropped before the warehouse.
    assert _build().cost_drag_ratio == pytest.approx(0.18)


def test_round_trip_cost_needs_every_leg_priced() -> None:
    assert _build().round_trip_cost_usd == pytest.approx(40.0)  # 0.10+0.10, x2, x100
    partial = _build(chain=_chain(_contract(505.0)))
    assert partial.round_trip_cost_usd is None


# --- 1.3 volatility state ----------------------------------------------------
def test_the_iv_term_structure_is_captured() -> None:
    mc = _build()
    assert mc.iv30 == pytest.approx(0.22)
    assert mc.term_structure_slope == pytest.approx(0.03)
    assert mc.iv_skew == pytest.approx(0.015)
    assert mc.iv_rank_source == "iv_history"


def test_implied_move_uses_the_traded_expiry_not_iv30() -> None:
    # The same horizon error the POP construct was fixed for: 30-day IV over a
    # short horizon overstates the move.
    mc = _build()
    expected, _ = implied_move(502.0, 0.24, 18)
    assert mc.implied_move_pct == pytest.approx(expected)


def test_implied_move_is_absent_without_an_iv() -> None:
    assert implied_move(502.0, None, 18) == (None, None)
    assert implied_move(None, 0.24, 18) == (None, None)


# --- 1.6 earnings ------------------------------------------------------------
def test_earnings_distance_is_recorded_in_days() -> None:
    assert _build().earnings_days_away == 32


def test_a_past_report_is_negative_not_clamped() -> None:
    # "-3" and "unknown" are different facts about a decision.
    assert _build(next_earnings=date(2026, 7, 31)).earnings_days_away == -3


def test_an_unknown_earnings_date_stays_unknown() -> None:
    mc = _build(next_earnings=None)
    assert mc.earnings_date is None and mc.earnings_days_away is None


# --- 1.7 depth ---------------------------------------------------------------
def test_volume_and_open_interest_are_captured_per_leg() -> None:
    lg = _build().legs[0]
    assert lg.volume == 1200 and lg.open_interest == 5400


# --- 1.8 Greeks: modeled, and labeled --------------------------------------
def test_greeks_are_computed_and_labeled_as_modeled() -> None:
    lg = _build().legs[0]
    assert lg.delta is not None and lg.gamma is not None
    assert lg.theta is not None and lg.vega is not None
    # THE honesty requirement: no provider in the stack supplies these.
    assert lg.greeks_source == GREEKS_MODELED


def test_greeks_are_absent_without_an_iv_rather_than_defaulted() -> None:
    mc = _build(chain=_chain(_contract(500.0, iv=None), _contract(505.0)))
    lg = next(x for x in mc.legs if x.strike == 500.0)
    assert lg.delta is None and lg.greeks_source == ""


def test_greeks_are_absent_without_a_spot() -> None:
    assert all(lg.delta is None for lg in _build(spot=None).legs)


def test_net_delta_refuses_to_sum_a_partial_structure() -> None:
    # A partial sum understates exposure while looking complete.
    assert _build().net_delta is not None
    assert _build(chain=_chain(_contract(505.0))).net_delta is None


# --- 1.9 realized vol / VRP --------------------------------------------------
def test_realized_vol_and_both_vrp_conventions_are_recorded() -> None:
    mc = _build()
    assert mc.realized_vol_20d == pytest.approx(0.18)
    assert mc.vrp_points == pytest.approx(0.04)  # 0.22 - 0.18
    assert mc.vrp_ratio == pytest.approx(0.22 / 0.18, rel=1e-3)


def test_vrp_is_absent_when_realized_vol_is() -> None:
    class _NoHV(_IV):
        hv20 = None

    mc = _build(iv_context=_NoHV())
    assert mc.vrp_points is None and mc.vrp_ratio is None


# --- 1.10 provenance ---------------------------------------------------------
def test_the_producing_build_is_stamped() -> None:
    mc = _build()
    # Empty only where git is unavailable; never a placeholder SHA.
    assert mc.signal_build_sha == "" or len(mc.signal_build_sha) >= 7
    assert mc.scoring_model_version == "sd-scoring-2026.07-v3"
    assert mc.chain_source == "unusual_whales"


# --- 1.11 regime -------------------------------------------------------------
def test_the_regime_tag_carries_vol_AND_tape() -> None:
    # A vol-only label cannot separate "bad model" from "bad week" — 52 of 67
    # audited signals were bearish into a strengthening tape.
    tag, v, t = regime_tag(0.45, [100.0 + i for i in range(30)])
    assert tag == "midvol/uptape" and v == "midvol" and t == "uptape"


def test_vol_regime_accepts_either_scale() -> None:
    assert vol_regime(0.85) == vol_regime(85.0) == "highvol"


def test_an_unknown_iv_rank_does_not_become_a_regime() -> None:
    assert vol_regime(None) == "unknown"
    assert regime_tag(None, None)[0] == "unknown"


def test_tape_needs_a_full_window_rather_than_estimating() -> None:
    assert tape_regime([100.0, 101.0, 102.0]) == "unknown"
    assert tape_regime(None) == "unknown"


def test_a_falling_tape_is_labeled_as_one() -> None:
    assert tape_regime([130.0 - i for i in range(30)]) == "downtape"


def test_a_flat_tape_is_neither() -> None:
    assert tape_regime([100.0] * 30) == "flat"


# --- The whole record degrades rather than fabricating -----------------------
def test_no_chain_at_all_still_produces_a_usable_record() -> None:
    mc = _build(chain=None, iv_context=None, spot=None)
    assert mc.legs and all(lg.bid is None for lg in mc.legs)
    assert mc.iv30 is None and mc.has_full_nbbo is False
    # Provenance and regime do not depend on the chain, so they must survive.
    assert mc.scoring_model_version == "sd-scoring-2026.07-v3"
    assert mc.regime_tape == "uptape"


def test_full_nbbo_is_only_true_when_every_leg_has_a_book() -> None:
    assert _build().has_full_nbbo is True
    assert _build(chain=_chain(_contract(505.0))).has_full_nbbo is False
