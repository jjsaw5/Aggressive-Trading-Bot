"""Metrics registry primitives + the aggregated /metrics endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.observability.metrics import Metrics

client = TestClient(app)


def test_counter_gauge_and_timing_summary() -> None:
    m = Metrics()
    m.inc("a")
    m.inc("a", 2)
    m.set_gauge("g", 4.5)
    for v in (10.0, 20.0, 30.0):
        m.observe("t", v)
    snap = m.snapshot()
    assert snap["counters"]["a"] == 3
    assert snap["gauges"]["g"] == 4.5
    t = snap["timings"]["t"]
    assert t["count"] == 3
    assert t["avg_ms"] == 20.0
    assert t["min_ms"] == 10.0 and t["max_ms"] == 30.0


def test_timer_records_a_sample() -> None:
    m = Metrics()
    with m.timer("block"):
        sum(range(1000))
    assert m.snapshot()["timings"]["block"]["count"] == 1


def test_metrics_endpoint_shape() -> None:
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.json()
    assert {"cache", "rate_limit", "events", "tiers", "registry"} <= set(body)
    assert {"broad", "watchlist", "candidates", "positions"} <= set(body["tiers"])
    assert {"counters", "gauges", "timings"} <= set(body["registry"])
