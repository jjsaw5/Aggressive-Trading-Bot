"""Typed events and their factory helpers.

One `Event` model carries a `type`, an optional `symbol`, a `payload` dict, and
provenance (`ts`, `source`). Keeping a single serializable model (rather than a
subclass per type) makes the bus, persistence, and a future Redis transport
trivial, while the factory helpers give call sites a typed, discoverable API.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EventType(str, Enum):
    # Market / signal
    PRICE_CHANGED = "price_changed"
    FLOW_DETECTED = "flow_detected"
    NEWS_PUBLISHED = "news_published"
    CATALYST_DETECTED = "catalyst_detected"
    MARKET_REGIME_CHANGED = "market_regime_changed"
    VOLATILITY_REGIME_CHANGED = "volatility_regime_changed"
    # Positions / orders
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    POSITION_UPDATED = "position_updated"
    ORDER_FILLED = "order_filled"
    ORDER_REJECTED = "order_rejected"
    PORTFOLIO_UPDATED = "portfolio_updated"
    RISK_THRESHOLD_REACHED = "risk_threshold_reached"
    # Infrastructure / health
    PROVIDER_FAILURE = "provider_failure"
    DATA_STALE = "data_stale"
    BROKER_MISMATCH = "broker_mismatch"


def _now() -> datetime:
    return datetime.now(UTC)


class Event(BaseModel):
    type: EventType
    symbol: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=_now)
    source: str = "system"

    def __str__(self) -> str:  # concise log line
        sym = f" {self.symbol}" if self.symbol else ""
        return f"<{self.type.value}{sym} {self.payload}>"


# --- Factory helpers (typed call sites) --------------------------------------
def price_changed(symbol: str, old: float, new: float, pct: float, *, source: str = "detector") -> Event:
    return Event(
        type=EventType.PRICE_CHANGED,
        symbol=symbol,
        payload={"old": old, "new": new, "pct": pct},
        source=source,
    )


def flow_detected(symbol: str, *, premium: float, count: int, source: str = "detector") -> Event:
    return Event(
        type=EventType.FLOW_DETECTED,
        symbol=symbol,
        payload={"premium": premium, "count": count},
        source=source,
    )


def catalyst_detected(symbol: str, *, event_type: str, event_date: str, source: str = "detector") -> Event:
    return Event(
        type=EventType.CATALYST_DETECTED,
        symbol=symbol,
        payload={"event_type": event_type, "event_date": event_date},
        source=source,
    )


def market_regime_changed(old: str, new: str, *, source: str = "detector") -> Event:
    return Event(
        type=EventType.MARKET_REGIME_CHANGED,
        payload={"old": old, "new": new},
        source=source,
    )


def volatility_regime_changed(old: str, new: str, *, source: str = "detector") -> Event:
    return Event(
        type=EventType.VOLATILITY_REGIME_CHANGED,
        payload={"old": old, "new": new},
        source=source,
    )


def provider_failure(provider: str, *, error: str, source: str = "health") -> Event:
    return Event(
        type=EventType.PROVIDER_FAILURE,
        payload={"provider": provider, "error": error},
        source=source,
    )


def data_stale(symbol: str | None, *, kind: str, age_seconds: float, source: str = "health") -> Event:
    return Event(
        type=EventType.DATA_STALE,
        symbol=symbol,
        payload={"kind": kind, "age_seconds": age_seconds},
        source=source,
    )
