"""Change detectors: emit an event only on material change."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.market import CatalystEvent
from app.domain.options import FlowAlert
from app.events.detectors import (
    CatalystDetector,
    FlowBurstDetector,
    PriceChangeDetector,
    RegimeDetector,
    classify_market_regime,
    classify_volatility_regime,
)
from app.events.types import EventType


def test_price_change_baseline_then_threshold() -> None:
    d = PriceChangeDetector(threshold_pct=1.0)
    assert d.detect("AAPL", 100.0) is None  # baseline, no event
    assert d.detect("AAPL", 100.5) is None  # +0.5% < 1% threshold
    evt = d.detect("AAPL", 102.0)  # +1.49% from 100.5
    assert evt is not None and evt.type == EventType.PRICE_CHANGED
    assert evt.symbol == "AAPL"
    assert evt.payload["pct"] > 1.0


def test_price_change_is_per_symbol() -> None:
    d = PriceChangeDetector(threshold_pct=2.0)
    d.detect("A", 100.0)
    d.detect("B", 50.0)
    assert d.detect("A", 103.0) is not None  # +3%
    assert d.detect("B", 50.5) is None  # +1% < 2%


def _alert(symbol: str, premium: float, ts: datetime) -> FlowAlert:
    return FlowAlert(symbol=symbol, premium=premium, ts=ts, source="test")


def test_flow_burst_baseline_then_fresh_large_print() -> None:
    d = FlowBurstDetector(min_premium_usd=250_000)
    t0 = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)
    # Baseline observation: no event even if large.
    assert d.detect("NVDA", [_alert("NVDA", 300_000, t0)]) is None
    # A newer, large print fires.
    t1 = t0 + timedelta(minutes=1)
    evt = d.detect("NVDA", [_alert("NVDA", 300_000, t0), _alert("NVDA", 400_000, t1)])
    assert evt is not None and evt.type == EventType.FLOW_DETECTED
    assert evt.payload["premium"] == 400_000 and evt.payload["count"] == 1


def test_flow_burst_below_threshold_no_event() -> None:
    d = FlowBurstDetector(min_premium_usd=1_000_000)
    t0 = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)
    d.detect("F", [_alert("F", 10_000, t0)])  # baseline
    t1 = t0 + timedelta(minutes=1)
    assert d.detect("F", [_alert("F", 20_000, t1)]) is None  # too small


def test_catalyst_detector_fires_once_per_new() -> None:
    d = CatalystDetector()
    c = CatalystEvent(symbol="AAPL", event_type="earnings", event_date="2026-08-01")
    first = d.detect("AAPL", [c])
    assert len(first) == 1 and first[0].type == EventType.CATALYST_DETECTED
    # Same catalyst again -> no repeat.
    assert d.detect("AAPL", [c]) == []
    # A new one fires.
    c2 = CatalystEvent(symbol="AAPL", event_type="dividend", event_date="2026-08-10")
    assert len(d.detect("AAPL", [c2])) == 1


def test_regime_detector_only_on_flip() -> None:
    d = RegimeDetector()
    assert d.detect("neutral") is None  # baseline
    assert d.detect("neutral") is None  # unchanged
    evt = d.detect("risk_on")
    assert evt is not None and evt.type == EventType.MARKET_REGIME_CHANGED
    assert evt.payload == {"old": "neutral", "new": "risk_on"}


def test_regime_classifiers() -> None:
    assert classify_market_regime(0.5) == "risk_on"
    assert classify_market_regime(-0.5) == "risk_off"
    assert classify_market_regime(0.0) == "neutral"
    assert classify_volatility_regime(0.8) == "high_vol"
    assert classify_volatility_regime(0.2) == "low_vol"
    assert classify_volatility_regime(0.5) == "mid_vol"
