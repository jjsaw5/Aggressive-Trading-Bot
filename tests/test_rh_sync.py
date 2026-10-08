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


# --- episode identity ---------------------------------------------------------
def test_same_contract_traded_twice_in_one_day_gets_two_ids() -> None:
    """The 2026-08-24 defect: same-day re-entries collided into one id.

    `trade_id` hashed (symbol, legs, open DATE). Two round trips on the same
    contract on the same day therefore produced one id, and `save_paper_trade`
    is last-write-wins — so the second round trip silently overwrote the first
    and its realized P&L left the corpus. Four of seven TSLA episodes on
    2026-08-24 were affected; the day was understated by $452.

    A losing round trip that vanishes is indistinguishable from one that never
    happened, which is the same failure as writing 0.0 for a missing price.
    """
    p = _payload(
        _order("o1", "TSLA", [
            ("buy", "open", "call", 355.0, "2026-08-24", 2.05, 1)], "2026-08-24T14:00:00Z"),
        _order("o2", "TSLA", [
            ("sell", "close", "call", 355.0, "2026-08-24", 3.40, 1)], "2026-08-24T15:00:00Z"),
        # Re-entered the SAME strike and expiry later the same session.
        _order("o3", "TSLA", [
            ("buy", "open", "call", 355.0, "2026-08-24", 2.65, 1)], "2026-08-24T17:00:00Z"),
        _order("o4", "TSLA", [
            ("sell", "close", "call", 355.0, "2026-08-24", 2.25, 1)], "2026-08-24T18:00:00Z"),
    )
    eps = rh_sync.build_episodes(rh_sync.load_fills(p), as_of=date(2026, 8, 25))
    assert len(eps) == 2, "two round trips, not one"

    ids = {e.trade_id() for e in eps}
    assert len(ids) == 2, f"same-day re-entry must not collide, got {ids}"

    # Both round trips survive, and they net to the sum — not to the last one.
    pnls = sorted(round((e.exit_net() - e.entry_net()) * 100, 2) for e in eps)
    assert pnls == [-40.0, 135.0]
    assert round(sum(pnls), 2) == 95.0


def test_trade_id_is_stable_across_reruns() -> None:
    """The fix must not cost idempotency — that is what the id is FOR.

    Re-keying on the opening order id keeps the id a pure function of broker
    data, so the scheduled open/close sync still creates nothing new on a
    re-run. A discriminator drawn from wall-clock or row order would break this.
    """
    p = _payload(
        _order("o1", "TSLA", [
            ("buy", "open", "put", 352.5, "2026-08-24", 1.71, 2)], "2026-08-24T14:30:00Z"),
        _order("o2", "TSLA", [
            ("sell", "close", "put", 352.5, "2026-08-24", 3.82, 2)], "2026-08-24T19:30:00Z"),
    )
    first = rh_sync.build_episodes(rh_sync.load_fills(p), as_of=date(2026, 8, 25))[0]
    again = rh_sync.build_episodes(rh_sync.load_fills(p), as_of=date(2026, 8, 25))[0]
    assert first.trade_id() == again.trade_id()
    # ...and re-running against a payload that has since grown more orders
    # must not move an existing episode's id.
    grown = _payload(*p["data"]["orders"], _order("o9", "AMD", [
        ("buy", "open", "call", 200.0, "2026-09-19", 1.00, 1)], "2026-08-25T14:00:00Z"))
    later = next(e for e in rh_sync.build_episodes(
        rh_sync.load_fills(grown), as_of=date(2026, 8, 26)) if e.symbol == "TSLA")
    assert later.trade_id() == first.trade_id()


def test_id_falls_back_to_the_open_timestamp_when_no_order_id() -> None:
    """Absent order id must not silently re-collide.

    `Fill.order_id` defaults to "". If a payload ever parses without one, the
    opening timestamp carries the same distinction at lower resolution — the
    one thing that must not happen is quietly reverting to the date-only key.
    """
    mk = lambda ts, px, eff, side: rh_sync.Fill(  # noqa: E731
        symbol="TSLA", ts=__import__("datetime").datetime.fromisoformat(ts),
        option_type="call", strike=355.0,
        expiration=date(2026, 8, 24), signed_qty=1.0 if side == "buy" else -1.0,
        price=px, effect=eff, order_id="")
    a = rh_sync.Episode(symbol="TSLA", fills=[
        mk("2026-08-24T14:00:00+00:00", 2.05, "open", "buy"),
        mk("2026-08-24T15:00:00+00:00", 3.40, "close", "sell")])
    b = rh_sync.Episode(symbol="TSLA", fills=[
        mk("2026-08-24T17:00:00+00:00", 2.65, "open", "buy"),
        mk("2026-08-24T18:00:00+00:00", 2.25, "close", "sell")])
    assert a.trade_id() != b.trade_id()


def test_one_tracked_row_cannot_absorb_two_round_trips(monkeypatch) -> None:
    """The second half of the same-day leak.

    `_matches_tracked` is deliberately fuzzy — same symbol, same contracts,
    opened within 5 days — so a manually-entered position still reconciles with
    its broker-side view. Unbounded, that fuzziness lets ONE tracked row claim
    every re-entry on the contract: the extra round trips report as
    `already_tracked` and their realized P&L never enters the corpus. Fixing
    `trade_id` alone would have moved the loss from an overwrite to a silent
    skip rather than removing it.
    """
    p = _payload(
        _order("o1", "TSLA", [
            ("buy", "open", "call", 355.0, "2026-08-24", 2.05, 1)], "2026-08-24T14:00:00Z"),
        _order("o2", "TSLA", [
            ("sell", "close", "call", 355.0, "2026-08-24", 3.40, 1)], "2026-08-24T15:00:00Z"),
        _order("o3", "TSLA", [
            ("buy", "open", "call", 355.0, "2026-08-24", 2.65, 1)], "2026-08-24T17:00:00Z"),
        _order("o4", "TSLA", [
            ("sell", "close", "call", 355.0, "2026-08-24", 2.25, 1)], "2026-08-24T18:00:00Z"),
    )
    eps = rh_sync.build_episodes(rh_sync.load_fills(p), as_of=date(2026, 8, 25))
    assert len(eps) == 2
    # Pretend the FIRST round trip is already tracked; the second must not be
    # swallowed by it.
    already = rh_sync.episode_to_trade(eps[0])

    class _Repo:
        @staticmethod
        def list_paper_trades(_n):
            return [already]

        @staticmethod
        def save_paper_trade(_t):
            raise AssertionError("dry-run must not write")

    import app.db.repository as real
    for name in ("list_paper_trades",):
        monkeypatch.setattr(real, name, getattr(_Repo, name))

    report = rh_sync.sync(p, apply=False)

    # Exactly ONE episode reconciles against the tracked row. Which bucket it
    # lands in depends on that row's status (here OPEN, so it closes in place);
    # the load-bearing part is that it claims one episode, not both.
    reconciled = report["already_tracked"] + report["closed_in_place"]
    assert len(reconciled) == 1, reconciled
    assert already.id in reconciled[0]

    # The other round trip survives as its own trade, carrying its own P&L.
    assert len(report["created_closed"]) == 1, report["created_closed"]
    assert "pnl -40.00" in report["created_closed"][0]
    assert already.id not in report["created_closed"][0]
