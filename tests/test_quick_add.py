"""Quick live-trade entry: one-line parser, endpoint, close→outcome grading."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.services.position_import import parse_trade_line

_TODAY = date(2026, 7, 23)


def test_parses_put_debit_spread() -> None:
    sym, legs, net, qty = parse_trade_line("TSLA 370/365p 7/24 @2.45 x1", today=_TODAY)
    assert sym == "TSLA" and net == 2.45 and qty == 1
    assert [(lg.strike, lg.is_long) for lg in legs] == [(370.0, True), (365.0, False)]
    assert legs[0].option_type.value == "put"
    assert legs[0].expiration == date(2026, 7, 24)


def test_parses_single_leg_and_defaults() -> None:
    sym, legs, net, qty = parse_trade_line("aapl 230c 8/15 3.10", today=_TODAY)
    assert (sym, net, qty) == ("AAPL", 3.10, 1)
    assert len(legs) == 1 and legs[0].is_long and legs[0].option_type.value == "call"


def test_parses_credit_spread_and_quantity() -> None:
    _sym, legs, net, qty = parse_trade_line("SPY 645/640c 12/19 @-1.55 x2", today=_TODAY)
    assert net == -1.55 and qty == 2
    assert [(lg.strike, lg.is_long) for lg in legs] == [(645.0, True), (640.0, False)]


def test_parses_decimal_strike_iso_date_and_year_rollover() -> None:
    _s, legs, _n, _q = parse_trade_line("TSLA 362.5c 2026-07-24 @17.65", today=_TODAY)
    assert legs[0].strike == 362.5 and legs[0].expiration == date(2026, 7, 24)
    # M/D already past this year -> rolls to next year.
    _s, legs, _n, _q = parse_trade_line("MSFT 500c 1/16 4.2", today=_TODAY)
    assert legs[0].expiration == date(2027, 1, 16)


def test_rejects_garbage_and_credit_single_leg() -> None:
    with pytest.raises(ValueError, match="Format"):
        parse_trade_line("not a trade", today=_TODAY)
    with pytest.raises(ValueError, match="type"):
        parse_trade_line("TSLA 370/365 7/24 @2.45", today=_TODAY)  # no c/p
    with pytest.raises(ValueError, match="credit"):
        parse_trade_line("AAPL 230c 8/15 -3.10", today=_TODAY)  # long option, credit net


def test_quick_add_close_grades_live_outcome() -> None:
    # End to end: quick-add -> LIVE snapshot warehoused -> close -> live_close
    # outcome -> calibration scorecard shows the live cohort with real P&L.
    import app.main as m
    from app.db import repository

    client = TestClient(m.app)
    r = client.post("/positions/quick-add", json={"line": "TSLA 370/365p 7/24 @2.45 x1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "TSLA" and "long 370P" in body["parsed"]
    tid = body["id"]

    snap = repository.get_snapshot(f"live:{tid}")
    assert snap is not None and snap.source.value == "live"

    r = client.post(f"/positions/{tid}/close",
                    json={"exit_price_per_share": 4.90, "reason": "hit target"})
    assert r.status_code == 200, r.text

    outs = repository.get_outcomes_for(f"live:{tid}")
    assert outs and outs[0].outcome_source == "live_close"
    assert outs[0].result.value == "win"
    assert outs[0].realized_pnl_usd == pytest.approx(245.0)  # (4.90-2.45)*100

    # The scorecard now carries a live cohort with the real win.
    from app.analytics.calibration import build_scorecard
    snaps, all_outs = repository.fetch_calibration_data(limit=2000)
    card = build_scorecard(snaps, all_outs)
    live = next((g for g in card.by_decision_source if g.key == "live"), None)
    assert live is not None and live.n >= 1 and (live.win_rate or 0) > 0


def test_bad_line_returns_usable_400() -> None:
    import app.main as m

    client = TestClient(m.app)
    r = client.post("/positions/quick-add", json={"line": "TSLA banana"})
    assert r.status_code == 400
    assert "Format" in r.json()["detail"]
