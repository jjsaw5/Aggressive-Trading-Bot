"""Warehouse persistence + the /outcomes API, against the mock-backed app."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.db import repository
from app.domain.enums import Direction, StrategyType
from app.domain.outcomes import DecisionSource
from app.main import app
from tests.test_outcomes import _snap

client = TestClient(app)


def test_snapshot_and_outcome_roundtrip() -> None:
    snap = _snap(
        strategy=StrategyType.BULL_CALL_SPREAD, direction=Direction.BULLISH,
        breakevens=[101.5], entry_spot=100.0, pop=0.4, decision_id="scanRT:AAA",
    )
    added = repository.save_snapshots([snap])
    assert added == 1
    # Idempotent: saving again adds nothing.
    assert repository.save_snapshots([snap]) == 0

    loaded = repository.get_snapshot("scanRT:AAA")
    assert loaded is not None and loaded.symbol == "AAA"

    pending = repository.list_snapshots(2000, status="pending")
    assert any(s.decision_id == "scanRT:AAA" for s in pending)

    from app.analytics.outcomes import resolve_underlying

    out = resolve_underlying(
        loaded, spot_now=110.0, resolved_at=datetime(2026, 7, 17, tzinfo=UTC)
    )
    repository.save_outcome(out)

    # Snapshot is promoted to resolved.
    assert not any(
        s.decision_id == "scanRT:AAA" for s in repository.list_snapshots(2000, status="pending")
    )
    got = repository.get_outcomes_for("scanRT:AAA")
    assert len(got) == 1 and got[0].result.value == "win"


def test_warehouse_from_scan_and_calibration_endpoint() -> None:
    # A scan should warehouse actionable decisions as a side effect.
    scan = client.post("/scans", json={"symbols": ["SPY", "AAPL", "NVDA"]},
                       params={"actionable_only": True})
    assert scan.status_code == 200

    snaps = client.get("/outcomes/snapshots", params={"limit": 100})
    assert snaps.status_code == 200

    # Resolve immediately (min_age_days=0) so the scorecard has data.
    resolved = client.post("/outcomes/resolve", params={"min_age_days": 0})
    assert resolved.status_code == 200
    assert resolved.json()["resolved"] >= 0

    card = client.get("/outcomes/calibration")
    assert card.status_code == 200
    body = card.json()
    assert "win_rate" in body and "pop_buckets" in body
    assert body["n_decisions"] >= body["n_resolved"]


def test_decision_detail_404() -> None:
    r = client.get("/outcomes/nope:XYZ")
    assert r.status_code == 404


def test_decision_detail_ok() -> None:
    snap = _snap(
        strategy=StrategyType.LONG_CALL, direction=Direction.BULLISH,
        breakevens=[101.5], entry_spot=100.0, decision_id="scanDET:AAA",
    )
    repository.save_snapshots([snap])
    r = client.get("/outcomes/scanDET:AAA")
    assert r.status_code == 200
    assert r.json()["snapshot"]["decision_id"] == "scanDET:AAA"
    assert r.json()["snapshot"]["source"] == DecisionSource.SCAN.value
