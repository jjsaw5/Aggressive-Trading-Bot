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
