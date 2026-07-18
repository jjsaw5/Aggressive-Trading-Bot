"""Importing real broker positions into tracked trades for Tier 4."""

from __future__ import annotations

from datetime import date

from app.domain.enums import OptionType, PaperTradeStatus, StrategyType
from app.services.position_import import ImportedLeg, build_tracked_trade


def test_import_bull_call_spread() -> None:
    # MSFT 400/410 call debit spread: long 400 @ 20.50, short 410 @ 15.82.
    legs = [
        ImportedLeg(400.0, OptionType.CALL, True, 1, 20.50, date(2026, 8, 7)),
        ImportedLeg(410.0, OptionType.CALL, False, 1, 15.82, date(2026, 8, 7)),
    ]
    trade = build_tracked_trade("MSFT", legs)
    plan = trade.trade_plan
    assert plan.strategy == StrategyType.BULL_CALL_SPREAD
    assert trade.entry_fill == 4.68  # net debit per share
    assert plan.risk.max_loss_usd == 468.0  # net debit x100
    assert plan.risk.max_profit_usd == 532.0  # (10 - 4.68) x100
    assert plan.exit_plan is not None and plan.exit_plan.levels
    assert trade.status == PaperTradeStatus.OPEN


def test_import_long_call() -> None:
    # NOW 107 call: long @ 2.62, uncapped upside.
    legs = [ImportedLeg(107.0, OptionType.CALL, True, 1, 2.62, date(2026, 7, 17))]
    trade = build_tracked_trade("NOW", legs)
    assert trade.trade_plan.strategy == StrategyType.LONG_CALL
    assert trade.entry_fill == 2.62
    assert trade.trade_plan.risk.max_loss_usd == 262.0
    assert trade.trade_plan.risk.max_profit_usd is None  # uncapped


def test_import_endpoint_and_positions_view() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    imported = client.post(
        "/paper/import",
        json=[{
            "symbol": "AAPL",
            "legs": [{
                "strike": 200.0, "option_type": "call", "is_long": True,
                "quantity": 1, "entry_price_per_share": 3.0, "expiration": "2026-09-18",
            }],
        }],
    )
    assert imported.status_code == 200
    assert imported.json()[0]["symbol"] == "AAPL"

    view = client.get("/positions")
    assert view.status_code == 200
    rows = view.json()
    aapl = next((r for r in rows if r["symbol"] == "AAPL"), None)
    assert aapl is not None
    assert aapl["action"] in {"hold", "take_profit", "stop", "time_stop", "unmarked"}
    # Enriched detail for the Positions page: expiration, risk economics, a
    # close ticket (reversed legs), the exit plan, and human-readable warnings.
    assert aapl["expiration"] == "2026-09-18"
    assert aapl["max_loss_usd"] == 300.0  # 3.00/share x100 x1
    assert aapl["time_stop_dte"] == 7
    assert aapl["legs"] and aapl["legs"][0]["side"] == "Sell to close"
    assert aapl["legs"][0]["contract"] == "200C"
    assert aapl["exit_levels"]  # standing reminder of where to close
    assert isinstance(aapl["warnings"], list)
    # Live risk-profile fields are always present (null when unmarkable).
    for k in ("net_delta", "net_theta", "breakeven_distance_pct", "underlying_price",
              "earnings_date"):
        assert k in aapl
    assert isinstance(aapl["earnings_before_expiry"], bool)
    assert aapl["breakevens"] == [203.0]  # 200 call + 3.00 debit


def test_positions_sync_reports_reason_when_broker_unavailable() -> None:
    # No brokerage that can read positions is configured in tests, so the sync
    # endpoint must fail gracefully with a clear reason (never a 500).
    from fastapi.testclient import TestClient

    from app.main import app

    resp = TestClient(app).post("/positions/sync")
    assert resp.status_code in {200, 400}
    if resp.status_code == 400:
        assert "sync unavailable" in resp.json()["detail"].lower()


def test_import_multi_contract_scales_risk() -> None:
    # SOFI 19/24 x2 contracts: net 1.26/share.
    legs = [
        ImportedLeg(19.0, OptionType.CALL, True, 2, 1.62, date(2026, 8, 21)),
        ImportedLeg(24.0, OptionType.CALL, False, 2, 0.36, date(2026, 8, 21)),
    ]
    trade = build_tracked_trade("SOFI", legs)
    assert trade.trade_plan.contracts == 2
    assert trade.trade_plan.risk.max_loss_usd == 252.0  # 1.26 x100 x2
