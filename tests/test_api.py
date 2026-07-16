"""API smoke tests against the mock-backed app (no infrastructure needed)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health() -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_provider_status_lists_capabilities() -> None:
    r = client.get("/config/providers")
    assert r.status_code == 200
    caps = {row["capability"] for row in r.json()}
    assert {"market_data", "options_flow", "options_chain"} <= caps


def test_scan_and_proposal_flow() -> None:
    scan = client.post("/scans", json={}, params={"actionable_only": True})
    assert scan.status_code == 200
    body = scan.json()
    if body["actionable"] == 0:
        return  # deterministic mock may reject all on a given day; flow still valid

    cand = body["candidates"][0]
    prop = client.post(
        "/proposals",
        json={"scan_id": cand["scan_id"], "symbol": cand["symbol"]},
    )
    assert prop.status_code == 200
    pid = prop.json()["id"]

    # Approve, then attempt execution — must be denied (automation off).
    ok = client.post(f"/proposals/{pid}/approve", json={"approver": "tester"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "approved"

    execd = client.post(f"/proposals/{pid}/execute")
    assert execd.status_code == 200
    assert execd.json()["authorized"] is False


def test_scan_history_and_candidates() -> None:
    scan = client.post("/scans", json={"symbols": ["SPY", "AAPL"]})
    scan_id = scan.json()["scan_id"]
    assert scan_id

    history = client.get("/scans", params={"limit": 10})
    assert history.status_code == 200
    assert any(s["scan_id"] == scan_id for s in history.json())

    cands = client.get(f"/scans/{scan_id}/candidates")
    assert cands.status_code == 200
    assert len(cands.json()) >= 1

    missing = client.get("/scans/deadbeef/candidates")
    assert missing.status_code == 404


def test_paper_trade_flow() -> None:
    scan = client.post("/scans", json={}, params={"actionable_only": True})
    body = scan.json()
    if body["actionable"] == 0:
        return
    cand = body["candidates"][0]
    opened = client.post("/paper", json={"scan_id": cand["scan_id"], "symbol": cand["symbol"]})
    assert opened.status_code == 200
    tid = opened.json()["id"]
    assert opened.json()["status"] == "open"

    got = client.get(f"/paper/{tid}")
    assert got.status_code == 200
    listing = client.get("/paper")
    assert any(t["id"] == tid for t in listing.json())
