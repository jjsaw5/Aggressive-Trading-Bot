"""Acceptance tests for the UW historical-options provider + real-mark harness.

Covers the build spec's Definition of Done (§11). The "live spike" is exercised
against a monkeypatched HTTP layer — we do not call the paid endpoint from CI —
but the entitlement path (401/403 -> HistoricalDataUnentitledError) and the full
parse/fill/causality/IV-crush machinery are all pinned here.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.analytics.metrics import max_drawdown
from app.backtest.fill_model import (
    leg_fill,
    round_trip_commission,
    round_trip_pnl,
    tradeable,
)
from app.backtest.iv_crush import bucket_pnl_by_move, measure_iv_crush
from app.backtest.real_mark import (
    ExitRule,
    RealMarkTrade,
    evaluate_real_mark_trade,
    horizon_fidelity,
    signal_window,
)
from app.domain.historic import HistoricOptionBar, parse_float, parse_int
from app.providers._http import ProviderHTTPError
from app.providers.unusual_whales.historical import (
    HistoricalDataUnentitledError,
    UWHistoricalOptionsProvider,
    _bar_from_row,
)


def _bar(d: date, bid, ask, *, iv=0.30, oi=1000, vol=100, trades=50) -> HistoricOptionBar:
    return HistoricOptionBar(
        contract_id="AAA260821C00100000", date=d, nbbo_bid=bid, nbbo_ask=ask,
        iv=iv, open_interest=oi, volume=vol, trades=trades,
    )


# --- §11.1 live spike + entitlement -----------------------------------------
async def test_history_parses_rows_and_drops_bad_dates() -> None:
    provider = UWHistoricalOptionsProvider()
    payload = {
        "chains": [
            {"date": "2026-05-26", "nbbo_bid": "0.90", "nbbo_ask": "1.10",
             "implied_volatility": "0.310502942482285", "open_interest": "1200",
             "volume": "300", "trades": "44", "last_price": "1.00"},
            {"date": "not-a-date", "nbbo_bid": "1.0", "nbbo_ask": "1.2"},  # dropped
        ]
    }

    async def fake_get_json(path, params=None):
        return payload

    provider._http.get_json = fake_get_json
    bars = await provider.get_contract_history("AAA260821C00100000")
    assert len(bars) == 1  # the malformed-date row is dropped, not coerced
    assert bars[0].date == date(2026, 5, 26)
    assert bars[0].iv == pytest.approx(0.310502942482285)
    assert bars[0].mid == pytest.approx(1.0)


async def test_unentitled_token_raises_loudly() -> None:
    provider = UWHistoricalOptionsProvider()

    async def fake_403(path, params=None):
        raise ProviderHTTPError("unusual_whales_historic", 403, path)

    provider._http.get_json = fake_403
    with pytest.raises(HistoricalDataUnentitledError):
        await provider.get_contract_history("AAA260821C00100000")


# --- §11.2 string parsing ----------------------------------------------------
def test_string_parsing_never_fabricates() -> None:
    assert parse_float("0.310502942482285") == pytest.approx(0.310502942482285)
    assert parse_float("garbage") is None  # not 0.0
    assert parse_float(None) is None
    assert parse_int("1200") == 1200
    # A malformed row yields None fields, never a fabricated value.
    bar = _bar_from_row("AAA", {"date": "2026-05-26", "nbbo_bid": "x", "nbbo_ask": None})
    assert bar is not None and bar.nbbo_bid is None and bar.nbbo_ask is None
    # A malformed date drops the whole row (no now() fallback).
    assert _bar_from_row("AAA", {"date": "nope"}) is None


# --- §11.3 crossed-quote reject ---------------------------------------------
def test_crossed_quote_is_not_tradeable() -> None:
    crossed = _bar(date(2026, 5, 26), bid=1.20, ask=1.00)  # ask < bid
    assert crossed.mid is None
    assert crossed.half_spread is None
    assert not tradeable(crossed)


# --- §11.4 fill model directions --------------------------------------------
def test_fill_crosses_the_spread_by_k() -> None:
    bar = _bar(date(2026, 5, 26), bid=1.00, ask=1.20)  # mid 1.10, half 0.10
    assert leg_fill(bar, "buy", 0.5) == pytest.approx(1.15)
    assert leg_fill(bar, "buy", 1.0) == pytest.approx(1.20)
    assert leg_fill(bar, "sell", 0.5) == pytest.approx(1.05)
    assert leg_fill(bar, "sell", 1.0) == pytest.approx(1.00)
    # buy >= mid, sell <= mid; k=1.0 strictly costlier than k=0.5.
    assert leg_fill(bar, "buy", 1.0) > leg_fill(bar, "buy", 0.5) >= bar.mid
    assert leg_fill(bar, "sell", 1.0) < leg_fill(bar, "sell", 0.5) <= bar.mid


# --- §11.5 round-trip commissions -------------------------------------------
def test_round_trip_charges_four_commission_legs_net_below_gross() -> None:
    assert round_trip_commission(contracts=1, per_contract=0.65) == pytest.approx(2.60)
    el = _bar(date(2026, 1, 5), 2.00, 2.10)
    es = _bar(date(2026, 1, 5), 1.00, 1.10)
    xl = _bar(date(2026, 1, 20), 2.60, 2.70)
    xs = _bar(date(2026, 1, 20), 1.05, 1.15)
    rt = round_trip_pnl(el, es, xl, xs, k=1.0, contracts=1, per_contract_commission=0.65)
    assert rt.fillable
    assert rt.commissions_usd == pytest.approx(2.60)
    assert rt.net_pnl_usd < rt.gross_pnl_usd  # commissions always drag net below gross


# --- §11.6 liquidity guard ---------------------------------------------------
def test_zero_volume_entry_is_excluded() -> None:
    dead = _bar(date(2026, 1, 5), 1.00, 1.10, vol=0)
    assert not tradeable(dead)
    live = _bar(date(2026, 1, 5), 1.00, 1.10, vol=100, oi=1000)
    assert tradeable(live)


def test_evaluator_excludes_illiquid_entry() -> None:
    trade = RealMarkTrade(
        trade_id="t1", long_id="L", short_id="S",
        entry_date=date(2026, 1, 5), dte_at_entry=30,
    )
    el = _bar(date(2026, 1, 5), 2.00, 2.10, oi=1)  # OI below guard
    es = _bar(date(2026, 1, 5), 1.00, 1.10)
    res = evaluate_real_mark_trade(trade, [el], [es], ExitRule(time_stop_days=1))
    assert not res.included and res.exclusion_reason == "entry_liquidity_guard"


# --- §11.7 causality ---------------------------------------------------------
def test_signal_window_rejects_lookahead() -> None:
    bars = [_bar(date(2026, 1, d), 1.0, 1.1) for d in (3, 4, 5, 6, 7)]
    win = signal_window(bars, date(2026, 1, 5))
    assert [b.date.day for b in win] == [3, 4, 5]  # nothing after entry


# --- §11.8 IV crush ----------------------------------------------------------
def test_iv_crush_shows_vol_collapse_and_mark_move() -> None:
    contract = "AAA260821P00100000"
    bars = [
        HistoricOptionBar(contract, date(2026, 1, 14), 2.00, 2.10, iv=0.80),  # pre
        HistoricOptionBar(contract, date(2026, 1, 16), 1.00, 1.10, iv=0.35),  # post
    ]
    obs = measure_iv_crush(bars, event_date=date(2026, 1, 15))
    assert obs.iv_pre == 0.80 and obs.iv_post == 0.35
    assert obs.crush == pytest.approx(0.45)  # vol collapsed
    assert obs.mark_change == pytest.approx(-1.0)  # long premium lost value


def test_pnl_bucketed_by_post_event_move_exposes_left_tail() -> None:
    # Put-credit-spread book: wins on flat/up moves, the tail pays on a big drop.
    pairs = [(120.0, 0.01), (120.0, 0.03), (120.0, -0.005), (-380.0, -0.09)]
    buckets = {b.label: b for b in bucket_pnl_by_move(pairs)}
    assert buckets["down_big"].worst_pnl_usd == pytest.approx(-380.0)
    assert buckets["flat"].win_rate == 1.0


# --- §11.9 drawdown ----------------------------------------------------------
def test_drawdown_is_reported() -> None:
    assert max_drawdown([100.0, -50.0, -80.0, 30.0]) == pytest.approx(130.0)


# --- §11.10 dual-k + slippage-fragile ---------------------------------------
def test_dual_k_flags_slippage_fragile() -> None:
    trade = RealMarkTrade(
        trade_id="frag", long_id="L", short_id="S",
        entry_date=date(2026, 1, 5), dte_at_entry=30,
    )
    # Tiny edge (0.06/sh) with 0.0125 half-spreads: survives k=0.5, dies at k=1.0.
    el = _bar(date(2026, 1, 5), 1.9875, 2.0125)
    es = _bar(date(2026, 1, 5), 0.9875, 1.0125)
    xl = _bar(date(2026, 1, 6), 1.9875, 2.0125)
    xs = _bar(date(2026, 1, 6), 0.9275, 0.9525)  # short leg dropped -> mark +0.06
    res = evaluate_real_mark_trade(
        trade, [el, xl], [es, xs], ExitRule(time_stop_days=1),
    )
    assert res.included
    assert set(res.by_k.keys()) == {0.5, 1.0}
    assert res.by_k[0.5].net_pnl_usd is not None and res.by_k[0.5].net_pnl_usd > 0
    assert res.by_k[1.0].net_pnl_usd is not None and res.by_k[1.0].net_pnl_usd <= 0
    assert res.slippage_fragile is True


# --- §11.11 horizon labels ---------------------------------------------------
def test_horizon_labels_are_honest() -> None:
    assert horizon_fidelity(0).verdict == "NOT_TESTABLE"
    assert horizon_fidelity(1).verdict == "NOT_TESTABLE"
    assert horizon_fidelity(3).verdict == "VALIDATING_COARSE"
    assert horizon_fidelity(30).verdict == "VALIDATING"
    assert horizon_fidelity(30).horizon == "swing"
