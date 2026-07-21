"""Manual position entry (no broker): import, close, and retained history."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _client() -> TestClient:
    return TestClient(app)


def _import_long_call(c, symbol="NVDA", strike=120.0, px=4.10, exp="2026-08-21"):
    return c.post("/positions/import", json={
        "symbol": symbol,
        "legs": [{"strike": strike, "option_type": "call", "is_long": True,
                  "quantity": 2, "entry_price_per_share": px, "expiration": exp}],
    })


def test_import_creates_a_tracked_position() -> None:
    c = _client()
    r = _import_long_call(c)
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "NVDA" and body["strategy"] == "Long Call"
    assert body["contracts"] == 2 and body["entry_net"] == 4.10
    assert body["max_loss_usd"] == 820.0  # 4.10 * 100 * 2


def test_import_spread_by_net_debit() -> None:
    # The real TSLA put debit spread: long 370P / short 365P, 7/24, net cost 2.45.
    # No per-leg entry prices needed — just the broker's average cost.
    c = _client()
    r = c.post("/positions/import", json={
        "symbol": "TSLA",
        "net_debit_per_share": 2.45,
        "legs": [
            {"strike": 370, "option_type": "put", "is_long": True, "quantity": 1, "expiration": "2026-07-24"},
            {"strike": 365, "option_type": "put", "is_long": False, "quantity": 1, "expiration": "2026-07-24"},
        ],
    })
    assert r.status_code == 200
    b = r.json()
    assert b["strategy"] == "Put Debit Spread"
    assert b["entry_net"] == 2.45
    assert b["max_loss_usd"] == 245.0  # the debit paid — matches Robinhood

    # Close for a loss (spread now worth 1.75): (1.75 - 2.45) * 100 = -$70 — matches.
    r2 = c.post(f"/positions/{b['id']}/close", json={"exit_price_per_share": 1.75, "reason": "bounce"})
    assert r2.json()["realized_pnl_usd"] == -70.0


def test_import_requires_net_or_leg_prices() -> None:
    c = _client()
    r = c.post("/positions/import", json={
        "symbol": "TSLA",
        "legs": [{"strike": 370, "option_type": "put", "is_long": True, "quantity": 1, "expiration": "2026-07-24"}],
    })
    assert r.status_code == 400  # no net and no per-leg price


def test_import_rejects_bad_option_type() -> None:
    c = _client()
    r = c.post("/positions/import", json={
        "symbol": "NVDA",
        "legs": [{"strike": 120, "option_type": "banana", "is_long": True,
                  "quantity": 1, "entry_price_per_share": 4.1, "expiration": "2026-08-21"}],
    })
    assert r.status_code == 400


def test_close_marks_realized_pnl_and_retains_history() -> None:
    c = _client()
    tid = _import_long_call(c, symbol="AMD", px=2.00).json()["id"]

    # Close for a net gain: (3.00 - 2.00) * 100 * 2 = +$200.
    r = c.post(f"/positions/{tid}/close", json={"exit_price_per_share": 3.00, "reason": "hit target"})
    assert r.status_code == 200
    closed = r.json()
    assert closed["realized_pnl_usd"] == 200.0
    assert closed["exit_reason"] == "manual" and closed["exit_note"] == "hit target"
    assert closed["hold_days"] is not None

    # Closing again is refused.
    assert c.post(f"/positions/{tid}/close", json={"exit_price_per_share": 3.0}).status_code == 409

    # Retained in history, filterable by source.
    hist = c.get("/positions/history?source=manual").json()
    assert any(h["id"] == tid and h["exit_note"] == "hit target" for h in hist)


def test_delete_removes_position_entirely() -> None:
    c = _client()
    tid = _import_long_call(c, symbol="INTC", px=1.50).json()["id"]
    # It's tracked...
    assert any(p["id"] == tid for p in c.get("/positions").json())
    # ...then deleted entirely (for purging bad data).
    r = c.request("DELETE", f"/positions/{tid}")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert not any(p["id"] == tid for p in c.get("/positions").json())
    # Deleting a missing id 404s; unlike close, nothing is retained in history.
    assert c.request("DELETE", f"/positions/{tid}").status_code == 404
    assert not any(h["id"] == tid for h in c.get("/positions/history").json())


def test_multiple_positions_on_same_symbol_both_tracked() -> None:
    c = _client()
    # A long call AND a long put on the same underlying — distinct structures.
    _import_long_call(c, symbol="TSLA", strike=400.0, exp="2026-08-21")
    c.post("/positions/import", json={
        "symbol": "TSLA",
        "legs": [{"strike": 360.0, "option_type": "put", "is_long": True,
                  "quantity": 1, "entry_price_per_share": 5.0, "expiration": "2026-08-21"}],
    })
    open_syms = [p for p in c.get("/positions").json() if p["symbol"] == "TSLA"]
    # Both structures survive the de-dupe (keyed by structure, not bare symbol).
    assert len(open_syms) >= 2
    # PositionView.strategy is the enum value (the UI maps it to the Robinhood label).
    assert {p["strategy"] for p in open_syms} >= {"long_call", "long_put"}
