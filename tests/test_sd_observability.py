"""Phase 6 — observability: signal metadata, exit-policy config, scan metrics."""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.enums import DTECategory

_NOW = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)


async def test_candidate_carries_signal_metadata() -> None:
    from app.shortduration.detection import run_detection

    cands = await run_detection(DTECategory.ZERO_DTE, now=_NOW)
    assert cands
    # ORB / VWAP detections attach structured diagnostics; at least one candidate
    # should surface a non-empty signal_metadata (e.g. confirmation_mode / vwap_quality).
    assert any(c.signal_metadata for c in cands)
    orb = next((c for c in cands if "confirmation_mode" in c.signal_metadata), None)
    if orb is not None:
        assert "breakout_buffer" in orb.signal_metadata


def test_exit_policy_endpoint() -> None:
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    c = TestClient(app)
    r = c.get("/short-duration/configuration/exit-policy")
    assert r.status_code == 200
    body = r.json()
    assert body["0dte"]["flatten_et"] == settings.short_duration_0dte_flatten_et
    assert body["momentum_stop_bars"] == settings.short_duration_momentum_stop_bars
    assert "1_5dte_time_stop_dte" in body


async def test_scan_records_metrics() -> None:
    from app.observability.metrics import get_metrics
    from app.shortduration.detection import run_detection

    await run_detection(DTECategory.ZERO_DTE, now=_NOW)
    counters = get_metrics().snapshot()["counters"]
    assert any(k.startswith("sd.scan.candidates.") for k in counters)
    assert any(k.startswith("sd.scan.tradeable.") for k in counters)
