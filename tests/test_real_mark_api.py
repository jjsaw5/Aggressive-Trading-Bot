"""The real-mark backtest API route is gated, not broken, when UW historic is off.

With no entitlement configured (the CI default), the route must return a clean
`available=false` + reason at HTTP 200 — never a 500 — so the dashboard can render
the "enable it" state instead of erroring.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_real_mark_route_gated_off_returns_reason() -> None:
    r = client.get("/backtest/real-mark?mode=engine")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["validating"] is False
    assert "UW_HISTORIC_ENABLED" in body["reason"]


def test_real_mark_route_rejects_bad_mode() -> None:
    r = client.get("/backtest/real-mark?mode=bogus")
    assert r.status_code == 422  # pattern-validated query param


def test_real_mark_route_accepts_both_regimes_and_rejects_bad() -> None:
    for regime in ("2021-22", "2023-24"):
        r = client.get(f"/backtest/real-mark?mode=engine&regime={regime}")
        assert r.status_code == 200 and r.json()["available"] is False  # gated off in CI
    assert client.get("/backtest/real-mark?regime=1999").status_code == 422
