"""Robinhood sync reconstruction: episodes, signs, expiry, order-linkage."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

from app.domain.enums import Direction, OptionType, StrategyType
from app.services.position_import import ImportedLeg, _infer

_spec = importlib.util.spec_from_file_location(
    "rh_sync", Path(__file__).resolve().parents[1] / "scripts" / "rh_sync.py")
assert _spec is not None and _spec.loader is not None
rh_sync = importlib.util.module_from_spec(_spec)
sys.modules["rh_sync"] = rh_sync  # dataclasses resolve fields via sys.modules
_spec.loader.exec_module(rh_sync)


def _order(oid, sym, legs, ts):
    return {
        "id": oid, "state": "filled", "chain_symbol": sym,
        "legs": [{
            "side": side, "position_effect": effect, "option_type": typ,
            "strike_price": str(strike), "expiration_date": exp,
            "executions": [{"price": str(price), "quantity": str(qty),
                            "timestamp": ts}],
        } for (side, effect, typ, strike, exp, price, qty) in legs],
    }


def _payload(*orders):
    return {"data": {"orders": list(orders)}}


def test_debit_spread_round_trip_signs_and_pnl() -> None:
    p = _payload(
        _order("o1", "TSLA", [
            ("buy", "open", "put", 370.0, "2026-07-24", 3.05, 1),
            ("sell", "open", "put", 365.0, "2026-07-24", 0.60, 1),
        ], "2026-07-20T14:30:00Z"),
        _order("o2", "TSLA", [
            ("sell", "close", "put", 370.0, "2026-07-24", 5.00, 1),
            ("buy", "close", "put", 365.0, "2026-07-24", 0.17, 1),
        ], "2026-07-23T13:30:00Z"),
    )
    eps = rh_sync.build_episodes(rh_sync.load_fills(p), as_of=date(2026, 7, 24))
    assert len(eps) == 1 and eps[0].is_closed()
    ep = eps[0]
    assert ep.entry_net() == pytest.approx(2.45)   # debit > 0
    assert ep.exit_net() == pytest.approx(4.83)    # credit received > 0
    t = rh_sync.episode_to_trade(ep)
    assert t.scan_id == "rh_sync" and t.id.startswith("rh")
    assert t.trade_plan.strategy == StrategyType.BEAR_PUT_SPREAD


def test_credit_spread_signs() -> None:
    p = _payload(
        _order("o1", "NFLX", [
            ("sell", "open", "put", 76.0, "2026-07-24", 2.50, 1),
            ("buy", "open", "put", 71.0, "2026-07-24", 0.61, 1),
        ], "2026-07-07T14:30:00Z"),
        _order("o2", "NFLX", [
            ("buy", "close", "put", 76.0, "2026-07-24", 2.60, 1),
            ("sell", "close", "put", 71.0, "2026-07-24", 0.16, 1),
        ], "2026-07-09T14:30:00Z"),
    )
    ep = rh_sync.build_episodes(rh_sync.load_fills(p), as_of=date(2026, 7, 24))[0]
    assert ep.entry_net() == pytest.approx(-1.89)  # credit collected
    assert ep.exit_net() == pytest.approx(-2.44)   # debit paid to close
    # realized = (exit - entry) * 100 = -55
    assert (ep.exit_net() - ep.entry_net()) * 100 == pytest.approx(-55.0)


def test_position_held_past_expiration_expires_worthless() -> None:
    p = _payload(_order("o1", "RIVN", [
        ("buy", "open", "call", 21.0, "2026-05-08", 0.15, 2)], "2026-04-27T14:30:00Z"))
    eps = rh_sync.build_episodes(rh_sync.load_fills(p), as_of=date(2026, 7, 24))
    assert len(eps) == 1 and eps[0].is_closed()
    assert eps[0].exit_net() == 0.0
    assert eps[0].exit_reason().value == "expiry"
    assert eps[0].closed_at.date() == date(2026, 5, 8)


def test_overlapping_independent_trades_stay_separate() -> None:
    # Two unrelated NVDA trades overlap in time; only order co-occurrence links
    # contracts, so they must come out as two episodes, not one lump.
    p = _payload(
        _order("o1", "NVDA", [
            ("buy", "open", "call", 215.0, "2026-06-05", 0.67, 1)], "2026-06-03T14:00:00Z"),
        _order("o2", "NVDA", [
            ("buy", "open", "call", 235.0, "2026-08-21", 5.00, 1),
            ("sell", "open", "call", 245.0, "2026-08-21", 1.77, 1),
        ], "2026-06-03T15:00:00Z"),
        _order("o3", "NVDA", [
            ("sell", "close", "call", 235.0, "2026-08-21", 1.50, 1),
            ("buy", "close", "call", 245.0, "2026-08-21", 0.74, 1),
        ], "2026-06-25T14:00:00Z"),
    )
    eps = rh_sync.build_episodes(rh_sync.load_fills(p), as_of=date(2026, 7, 24))
    assert len(eps) == 2
    single = next(e for e in eps if len(e.opening_legs()) == 1)
    spread = next(e for e in eps if len(e.opening_legs()) == 2)
    assert single.exit_reason().value == "expiry"  # held past 6/5
    assert spread.exit_net() == pytest.approx(0.76)


def test_unrecognized_combo_splits_per_contract() -> None:
    # Two long calls bought in one order: no spread economics — falls back to
    # two independent single-leg trades.
    p = _payload(
        _order("o1", "SPCE", [
            ("buy", "open", "call", 7.0, "2026-06-18", 3.00, 1),
            ("buy", "open", "call", 8.0, "2026-06-18", 2.47, 1),
        ], "2026-06-01T14:00:00Z"),
        _order("o2", "SPCE", [
            ("sell", "close", "call", 7.0, "2026-06-18", 3.10, 1),
            ("sell", "close", "call", 8.0, "2026-06-18", 2.30, 1),
        ], "2026-06-01T15:00:00Z"),
    )
    ep = rh_sync.build_episodes(rh_sync.load_fills(p), as_of=date(2026, 7, 24))[0]
    with pytest.raises(ValueError):
        rh_sync.episode_to_trade(ep)
    subs = rh_sync.split_per_contract(ep, as_of=date(2026, 7, 24))
    assert len(subs) == 2
    pnls = sorted(round((s.exit_net() - s.entry_net()) * 100, 2) for s in subs)
    assert pnls == [-17.0, 10.0]


# --- extended structure inference ---------------------------------------------
def _leg(typ, strike, long):
    return ImportedLeg(strike=strike, option_type=typ, is_long=long,
                       quantity=1, entry_price_per_share=1.0,
                       expiration=date(2026, 8, 21))


def test_infer_credit_verticals_and_straddle() -> None:
    assert _infer([_leg(OptionType.PUT, 76.0, False), _leg(OptionType.PUT, 71.0, True)]) \
        == (StrategyType.BULL_PUT_SPREAD, Direction.BULLISH)
    assert _infer([_leg(OptionType.CALL, 100.0, False), _leg(OptionType.CALL, 110.0, True)]) \
        == (StrategyType.BEAR_CALL_SPREAD, Direction.BEARISH)
    assert _infer([_leg(OptionType.CALL, 100.0, True), _leg(OptionType.PUT, 100.0, True)]) \
        == (StrategyType.LONG_STRADDLE, Direction.NEUTRAL)
    assert _infer([_leg(OptionType.CALL, 105.0, True), _leg(OptionType.PUT, 95.0, True)]) \
        == (StrategyType.LONG_STRANGLE, Direction.NEUTRAL)
