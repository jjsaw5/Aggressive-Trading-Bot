"""Event wiring: run change detectors over scan output and publish to the bus.

Additive by design. Today it runs price + market-regime detection over each
scan's candidates and publishes the resulting events; the default subscriber
just logs them (observability). It does NOT yet trigger recompute — that arrives
with the tier funnel, when tier evaluators subscribe to these events and react.
The detectors keep last-seen state across scans, so events fire only on material
change between ticks.
"""

from __future__ import annotations

from app.config import settings
from app.domain.candidates import TradeCandidate
from app.events.bus import get_event_bus
from app.events.detectors import (
    PriceChangeDetector,
    RegimeDetector,
    classify_market_regime,
)
from app.events.types import Event
from app.logging_config import get_logger

log = get_logger(__name__)

# Detectors persist across scans (module-level singletons) so "changed since the
# last tick" is meaningful.
_price = PriceChangeDetector(threshold_pct=settings.price_change_threshold_pct)
_regime = RegimeDetector()
_proxy_last: dict[str, float] = {}  # last-seen spot for the market-regime proxy
_MARKET_PROXIES = ("SPY", "QQQ")

_default_subscriber_registered = False


async def _log_subscriber(event: Event) -> None:
    log.info("event", type=event.type.value, symbol=event.symbol, payload=event.payload)


def ensure_default_subscriber() -> None:
    """Register the observability log subscriber once (idempotent)."""
    global _default_subscriber_registered
    if not _default_subscriber_registered:
        get_event_bus().subscribe_all(_log_subscriber)
        _default_subscriber_registered = True


def _entry_spot(c: TradeCandidate) -> float | None:
    plan = c.trade_plan
    if plan is None or plan.analytics is None:
        return None
    return plan.analytics.spot_at_analysis


async def publish_scan_events(candidates: list[TradeCandidate]) -> int:
    """Run detectors over a scan's candidates and publish events. Returns count."""
    if not settings.events_enabled or not candidates:
        return 0
    ensure_default_subscriber()
    bus = get_event_bus()
    published = 0

    # Per-symbol price moves since the last scan.
    for c in candidates:
        spot = _entry_spot(c)
        if spot is None:
            continue
        evt = _price.detect(c.symbol.upper(), spot)
        if evt is not None:
            await bus.publish(evt)
            published += 1

    # Market-regime shift from a broad-index proxy, if present in this scan.
    proxy = next(
        (c for sym in _MARKET_PROXIES for c in candidates if c.symbol.upper() == sym),
        None,
    )
    if proxy is not None:
        spot = _entry_spot(proxy)
        sym = proxy.symbol.upper()
        prev = _proxy_last.get(sym)
        if spot:
            _proxy_last[sym] = spot
        if spot and prev:
            trend_pct = (spot - prev) / prev * 100.0
            evt = _regime.detect(classify_market_regime(trend_pct))
            if evt is not None:
                await bus.publish(evt)
                published += 1

    if published:
        log.info("scan_events_published", count=published)
    return published
