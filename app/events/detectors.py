"""Change detectors: turn raw observations into events, only on material change.

Each detector holds the last-seen state per key and emits an `Event` only when
something meaningful changed — a price move past a threshold, a fresh large
flow print, a regime label flip, a newly-seen catalyst. This is the mechanism
behind "recalculate when price changes materially / new flow arrives" rather
than recomputing on every timer tick.

Detectors are deliberately pure and synchronous (no I/O, no bus coupling): they
take an observation and return an event or None. A caller publishes the result.
State is in-memory; a persistent store can back it later without changing the
detect() contract.
"""

from __future__ import annotations

from datetime import datetime

from app.domain.market import CatalystEvent
from app.domain.options import FlowAlert
from app.events.types import (
    Event,
    catalyst_detected,
    flow_detected,
    market_regime_changed,
    price_changed,
    volatility_regime_changed,
)


class PriceChangeDetector:
    """Emits PriceChanged when |Δ%| since last observation ≥ threshold_pct."""

    def __init__(self, threshold_pct: float = 1.0) -> None:
        self.threshold_pct = threshold_pct
        self._last: dict[str, float] = {}

    def detect(self, symbol: str, price: float) -> Event | None:
        prev = self._last.get(symbol)
        self._last[symbol] = price
        if prev is None or prev <= 0:
            return None  # baseline first observation
        pct = (price - prev) / prev * 100.0
        if abs(pct) >= self.threshold_pct:
            return price_changed(symbol, round(prev, 4), round(price, 4), round(pct, 4))
        return None


class FlowBurstDetector:
    """Emits FlowDetected when NEW flow prints (since last seen) total at least
    `min_premium_usd`. The first observation per symbol is a baseline (no event)
    so we react to *new* flow, not history."""

    def __init__(self, min_premium_usd: float = 250_000.0) -> None:
        self.min_premium_usd = min_premium_usd
        self._last_ts: dict[str, datetime] = {}

    def detect(self, symbol: str, alerts: list[FlowAlert]) -> Event | None:
        ts_values = [a.ts for a in alerts if a.ts is not None]
        newest = max(ts_values, default=None)
        prev = self._last_ts.get(symbol)
        if newest is not None:
            self._last_ts[symbol] = newest
        if prev is None:
            return None  # baseline
        fresh = [a for a in alerts if a.ts is not None and a.ts > prev]
        premium = sum(a.premium or 0.0 for a in fresh)
        if premium >= self.min_premium_usd and fresh:
            return flow_detected(symbol, premium=round(premium, 2), count=len(fresh))
        return None


class CatalystDetector:
    """Emits CatalystDetected once per newly-seen (symbol, date, type) catalyst."""

    def __init__(self) -> None:
        self._seen: set[tuple[str, str, str]] = set()

    def detect(self, symbol: str, catalysts: list[CatalystEvent]) -> list[Event]:
        out: list[Event] = []
        for c in catalysts:
            key = (symbol.upper(), str(c.event_date), c.event_type)
            if key in self._seen:
                continue
            self._seen.add(key)
            out.append(
                catalyst_detected(
                    symbol.upper(), event_type=c.event_type, event_date=str(c.event_date)
                )
            )
        return out


class LabelChangeDetector:
    """Base: emits an event only when a discrete label flips (skips the first)."""

    def __init__(self) -> None:
        self._last: str | None = None

    def _detect(self, label: str, make_event) -> Event | None:
        prev = self._last
        self._last = label
        if prev is not None and label != prev:
            return make_event(prev, label)
        return None


class RegimeDetector(LabelChangeDetector):
    def detect(self, label: str) -> Event | None:
        return self._detect(label, market_regime_changed)


class VolatilityRegimeDetector(LabelChangeDetector):
    def detect(self, label: str) -> Event | None:
        return self._detect(label, volatility_regime_changed)


def classify_market_regime(trend_pct: float, *, up: float = 0.3, down: float = -0.3) -> str:
    """Coarse market-regime label from a breadth/trend proxy (e.g. SPY %)."""
    if trend_pct >= up:
        return "risk_on"
    if trend_pct <= down:
        return "risk_off"
    return "neutral"


def classify_volatility_regime(iv_rank: float) -> str:
    """Coarse volatility-regime label from IV rank [0, 1]."""
    if iv_rank >= 0.66:
        return "high_vol"
    if iv_rank <= 0.33:
        return "low_vol"
    return "mid_vol"
