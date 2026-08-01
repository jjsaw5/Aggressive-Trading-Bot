"""What an intraday replay must refuse to do.

Phase 2 of the remediation directive. A minute-resolution replay is more
powerful than the daily one, and every extra degree of freedom it gains is a new
way to flatter the strategy. These tests pin the discipline:

  * A minute bar has a high and a low and NO ordering between them. Assuming the
    favourable one came first is the single easiest way to manufacture a
    backtest edge, so the stop is checked on the bar's worst price BEFORE the
    target is checked on its best.
  * Bars are sparse — only traded minutes exist. A gap is "no print", never
    "unchanged", and must never be interpolated.
  * A partially-priced structure is a different instrument, not an approximation.
  * MFE/MAE from bar extremes are BOUNDS, and the code must not present them as
    achieved prices.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.analytics.intraday_settlement import (
    settle_intraday,
    structure_range,
    walk_intraday,
)
from app.analytics.policy_settlement import occ_symbol
from app.domain.enums import Direction, OptionAction, OptionType, StrategyType
from app.domain.options import OptionMinuteBar
from app.domain.outcomes import DecisionSnapshot
from app.domain.trades import ContractLeg, RiskPlan, TradePlan

_EXP = date(2026, 8, 21)
_ENTRY = datetime(2026, 8, 3, 13, 30, tzinfo=UTC)
_LONG = occ_symbol("SPY", _EXP, OptionType.CALL, 500.0)
_SHORT = occ_symbol("SPY", _EXP, OptionType.CALL, 505.0)


def _plan(*, target=0.5, stop=0.5) -> TradePlan:
    return TradePlan(
        symbol="SPY", direction=Direction.BULLISH, strategy=StrategyType.BULL_CALL_SPREAD,
        legs=[
            ContractLeg(symbol="SPY", action=OptionAction.BUY_TO_OPEN,
                        option_type=OptionType.CALL, strike=500.0, expiration=_EXP,
                        quantity=1, entry_price=3.0),
            ContractLeg(symbol="SPY", action=OptionAction.SELL_TO_OPEN,
                        option_type=OptionType.CALL, strike=505.0, expiration=_EXP,
                        quantity=1, entry_price=2.0),
        ],
        net_debit=100.0, contracts=1,
        risk=RiskPlan(max_loss_usd=100.0, account_risk_pct=0.05,
                      profit_target_pct=target, stop_loss_pct=stop, time_stop_dte=2),
    )


def _bar(sym: str, minute: int, o, h, low, c, *, bid_v=10, ask_v=10,
         bid_p=None, ask_p=None) -> OptionMinuteBar:
    px = (o + c) / 2
    return OptionMinuteBar(
        option_symbol=sym,
        start_time=datetime(2026, 8, 3, 14, minute, tzinfo=UTC),
        open=o, high=h, low=low, close=c,
        volume_bid_side=bid_v, volume_ask_side=ask_v,
        premium_bid_side=(bid_p if bid_p is not None else px * 0.98) * bid_v * 100,
        premium_ask_side=(ask_p if ask_p is not None else px * 1.02) * ask_v * 100,
    )


def _minutes(*rows) -> dict:
    """rows: (minute, long_ohlc, short_ohlc)"""
    out: dict = {}
    for minute, lo, so in rows:
        ts = datetime(2026, 8, 3, 14, minute, tzinfo=UTC)
        out[ts] = {
            _LONG: _bar(_LONG, minute, *lo),
            _SHORT: _bar(_SHORT, minute, *so),
        }
    return out


def _snapshot() -> DecisionSnapshot:
    return DecisionSnapshot(
        decision_id="sd:x", scan_id="sd:x", symbol="SPY",
        direction=Direction.BULLISH, strategy=StrategyType.BULL_CALL_SPREAD,
        generated_at=_ENTRY, composite_score=0.7, entry_spot=502.0,
        entry_net_per_share=1.0, max_loss_usd=100.0, contracts=1,
        expiration=_EXP, dte_at_entry=18, trade_plan=_plan(),
    )


# --- The bar-ordering rule ----------------------------------------------------
def test_a_minute_that_traded_through_both_levels_is_a_loss() -> None:
    """THE rule. Entry net 1.00, target +50% (1.50), stop -50% (0.50).

    This minute's range spans both. We cannot know which printed first, so it
    books as the stop. Assuming otherwise is how a backtest invents an edge.
    """
    bars = _minutes((0, (3.0, 4.0, 2.0, 3.0), (2.0, 2.5, 1.5, 2.0)))
    exit_, _ = walk_intraday(_plan(), 1.0, bars)
    assert exit_ is not None and exit_.reason == "stop_loss"


def test_the_target_still_fires_when_the_stop_cannot() -> None:
    # Range entirely above entry: worst case never reaches the stop.
    bars = _minutes((0, (3.6, 3.8, 3.5, 3.7), (2.0, 2.1, 1.9, 2.0)))
    exit_, _ = walk_intraday(_plan(), 1.0, bars)
    assert exit_ is not None and exit_.reason == "profit_target"


def test_the_stop_is_priced_at_the_bar_low_not_its_close() -> None:
    # A bar that dipped to the stop and recovered still exits, at the worse price.
    bars = _minutes((0, (3.0, 3.1, 2.2, 3.0), (2.0, 2.1, 1.9, 2.0)))
    exit_, _ = walk_intraday(_plan(), 1.0, bars)
    assert exit_ is not None and exit_.reason == "stop_loss"
    assert exit_.exit_net == pytest.approx(2.2 - 2.1)


def test_the_first_triggering_minute_wins_not_the_best_one() -> None:
    bars = _minutes(
        (0, (3.0, 3.1, 2.9, 3.0), (2.0, 2.1, 1.9, 2.0)),   # quiet
        (1, (3.0, 3.1, 2.2, 3.0), (2.0, 2.1, 1.9, 2.0)),   # stop
        (2, (4.0, 4.2, 3.9, 4.1), (2.0, 2.1, 1.9, 2.0)),   # would have won
    )
    exit_, _ = walk_intraday(_plan(), 1.0, bars)
    assert exit_.reason == "stop_loss"
    assert exit_.exit_ts.minute == 1


# --- Sparse bars --------------------------------------------------------------
def test_a_minute_missing_one_leg_is_held_through_not_priced() -> None:
    ts = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
    bars = {ts: {_LONG: _bar(_LONG, 0, 3.0, 4.0, 2.0, 3.0)}}  # short leg absent
    exit_, exc = walk_intraday(_plan(), 1.0, bars)
    assert exit_ is None
    assert exc is None  # nothing was priced, so nothing was observed


def test_a_partially_quoted_structure_has_no_range() -> None:
    assert structure_range(_plan(), {_LONG: _bar(_LONG, 0, 3.0, 4.0, 2.0, 3.0)}) is None


def test_gaps_do_not_stop_a_later_minute_from_triggering() -> None:
    bars = _minutes(
        (0, (3.0, 3.1, 2.9, 3.0), (2.0, 2.1, 1.9, 2.0)),
        (45, (3.7, 3.8, 3.6, 3.7), (2.0, 2.1, 1.9, 2.0)),  # 44-minute gap
    )
    exit_, _ = walk_intraday(_plan(), 1.0, bars)
    assert exit_ is not None and exit_.exit_ts.minute == 45


def test_no_trigger_and_no_forced_close_leaves_the_trade_open() -> None:
    # "Still open" is a different claim from "expired", and must not be conflated.
    bars = _minutes((0, (3.0, 3.1, 2.9, 3.0), (2.0, 2.1, 1.9, 2.0)))
    exit_, exc = walk_intraday(_plan(), 1.0, bars)
    assert exit_ is None and exc is not None and exc.bars_seen == 1


def test_the_0dte_forced_close_books_the_last_priced_bar() -> None:
    bars = _minutes(
        (0, (3.0, 3.1, 2.9, 3.0), (2.0, 2.1, 1.9, 2.0)),
        (5, (3.2, 3.3, 3.1, 3.2), (2.0, 2.1, 1.9, 2.0)),
    )
    exit_, _ = walk_intraday(_plan(), 1.0, bars, session_close_exit=True)
    assert exit_ is not None and exit_.reason == "session_close"
    assert exit_.exit_ts.minute == 5


# --- Structure range ----------------------------------------------------------
def test_the_worst_case_pairs_long_low_with_short_high() -> None:
    bars = {_LONG: _bar(_LONG, 0, 3.0, 4.0, 2.0, 3.0), _SHORT: _bar(_SHORT, 0, 2.0, 2.5, 1.5, 2.0)}
    worst, best = structure_range(_plan(), bars)
    assert worst == pytest.approx(2.0 - 2.5)  # long low, short high
    assert best == pytest.approx(4.0 - 1.5)   # long high, short low


# --- Excursion ----------------------------------------------------------------
def test_excursion_records_both_extremes_and_when_they_happened() -> None:
    bars = _minutes(
        (0, (3.0, 3.2, 2.8, 3.0), (2.0, 2.0, 2.0, 2.0)),   # net 0.8 .. 1.2
        (1, (3.0, 3.4, 2.9, 3.0), (2.0, 2.0, 2.0, 2.0)),   # net 0.9 .. 1.4
    )
    # Unreachable thresholds so the walk runs the whole path without exiting;
    # RiskPlan requires floats, so "never fires" is expressed as a huge number.
    _exit, exc = walk_intraday(_plan(target=99.0, stop=99.0), 1.0, bars)
    assert exc.bars_seen == 2
    assert exc.mfe_per_share == pytest.approx(0.4)   # (1.4 - 1.0)/1.0
    assert exc.mae_per_share == pytest.approx(-0.2)  # (0.8 - 1.0)/1.0
    assert exc.mfe_ts.minute == 1 and exc.mae_ts.minute == 0


def test_excursion_is_tracked_even_on_the_bar_that_exits() -> None:
    bars = _minutes((0, (3.0, 4.0, 2.0, 3.0), (2.0, 2.5, 1.5, 2.0)))
    _exit, exc = walk_intraday(_plan(), 1.0, bars)
    assert exc is not None and exc.bars_seen == 1


# --- Full settlement ----------------------------------------------------------
def test_the_outcome_carries_exit_price_time_and_excursion() -> None:
    bars = _minutes((30, (3.0, 3.1, 2.2, 3.0), (2.0, 2.1, 1.9, 2.0)))
    o = settle_intraday(_snapshot(), bars, include_costs=False)
    assert o is not None
    assert o.exit_reason == "stop_loss"
    assert o.exit_ts is not None and o.exit_price_per_share is not None
    assert o.mfe_per_share is not None and o.mae_per_share is not None
    assert o.bars_observed == 1


def test_hold_minutes_is_measured_from_entry_to_exit() -> None:
    bars = _minutes((30, (3.0, 3.1, 2.2, 3.0), (2.0, 2.1, 1.9, 2.0)))
    o = settle_intraday(_snapshot(), bars, include_costs=False)
    assert o.hold_minutes == 60  # 13:30Z entry -> 14:30Z exit


def test_the_grade_is_labeled_intraday_so_it_never_pools_with_daily() -> None:
    bars = _minutes((30, (3.0, 3.1, 2.2, 3.0), (2.0, 2.1, 1.9, 2.0)))
    o = settle_intraday(_snapshot(), bars, include_costs=False)
    assert o.outcome_source == "managed_policy_intraday"
    assert o.horizon_label == "managed_exit_intraday"


def test_an_unresolved_decision_returns_nothing_rather_than_a_zero_grade() -> None:
    bars = _minutes((30, (3.0, 3.1, 2.9, 3.0), (2.0, 2.1, 1.9, 2.0)))
    assert settle_intraday(_snapshot(), bars, include_costs=False) is None


def test_bars_before_entry_are_ignored() -> None:
    # A stop that "fired" an hour before the signal existed is not evidence.
    early = {
        datetime(2026, 8, 3, 12, 0, tzinfo=UTC): {
            _LONG: _bar(_LONG, 0, 3.0, 3.1, 2.0, 3.0),
            _SHORT: _bar(_SHORT, 0, 2.0, 2.1, 1.9, 2.0),
        }
    }
    assert settle_intraday(_snapshot(), early, include_costs=False) is None
