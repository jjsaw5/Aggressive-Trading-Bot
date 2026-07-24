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


# --- Broker-paste entry (Robinhood position screen) ---------------------------
_JPM_PASTE = """Your position
Market value
$56.00
Current price	$0.28
Current JPM price	$349.94
Today's return	-$42.00 (-42.86%)
Total return	+$6.00 (+12.00%)
Expiration date
7/24
Average cost	$0.25
JPM breakeven price	$352.75
Quantity	+2
Date opened	7/23
Options
JPM $355 Call
7/24 · 2 Sells
$0.07
JPM $352.5 Call
7/24 · 2 Buys
$0.37
History
JPM Call Debit Spread
Individual · 20h	$50.00
"""


def test_broker_paste_parses_the_jpm_screen() -> None:
    from app.services.position_import import parse_broker_paste

    sym, legs, net, qty, opened = parse_broker_paste(_JPM_PASTE, today=_TODAY)
    assert sym == "JPM" and net == 0.25 and qty == 2
    assert opened == date(2026, 7, 23)
    by_strike = {lg.strike: lg for lg in legs}
    assert by_strike[352.5].is_long and not by_strike[355.0].is_long
    assert all(lg.option_type.value == "call" and lg.expiration == date(2026, 7, 24)
               for lg in legs)
    # The $0.07 / $0.37 current marks must NEVER be treated as entry prices.
    assert all(lg.entry_price_per_share == 0.0 for lg in legs)


def test_broker_paste_negative_quantity_means_credit() -> None:
    from app.services.position_import import parse_broker_paste

    text = ("Average cost $1.55\nQuantity -2\n"
            "SPY $640 Call\n12/19 · 2 Sells\n$1.00\n"
            "SPY $645 Call\n12/19 · 2 Buys\n$0.40\n")
    _s, _l, net, qty, _o = parse_broker_paste(text, today=_TODAY)
    assert net == -1.55 and qty == 2  # sold structure -> the cost is a credit


def test_broker_paste_missing_cost_is_a_clear_error() -> None:
    from app.services.position_import import parse_broker_paste

    with pytest.raises(ValueError, match="Average cost"):
        parse_broker_paste("JPM $355 Call\n7/24 · 2 Sells\nJPM $352.5 Call\n7/24 · 2 Buys",
                           today=_TODAY)


def test_quick_add_endpoint_autodetects_paste() -> None:
    import app.main as m
    from app.db import repository

    client = TestClient(m.app)
    r = client.post("/positions/quick-add", json={"line": _JPM_PASTE})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "JPM"
    assert "Bull Call Spread" in body["strategy"] or "call" in body["strategy"].lower()
    assert "long 352.5C" in body["parsed"] and "short 355C" in body["parsed"]
    assert "+0.25" in body["parsed"] and "x2" in body["parsed"]
    t = repository.get_paper_trade(body["id"])
    assert t is not None and t.opened_at.date() == date(2026, 7, 23)
    repository.delete_paper_trade(body["id"])
