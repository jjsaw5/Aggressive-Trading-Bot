"""Short-duration orchestration service (read-only market context).

Fetches data through the provider registry, computes intraday levels + breadth +
regime, and pulls news/events. Kept out of the API layer so routes stay thin and
the logic is unit-testable with a mock provider. Concurrency is bounded and open
positions are not touched here (Tier 4 owns those).

Strategy detection lives in `app/shortduration/detection.py` (Phase 2), which
reuses `build_market_regime` from here. This module stays detection-free — it is
the shared market-context layer.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.domain.internals import MarketInternals
from app.domain.shortduration import (
    EconomicEvent,
    IntradayLevels,
    NewsItem,
    ShortDurationRegimeState,
)
from app.engine.universe import DEFAULT_UNIVERSE
from app.logging_config import get_logger
from app.providers import registry
from app.shortduration.breadth import WatchlistParticipation, compute_participation
from app.shortduration.levels import compute_intraday_levels
from app.shortduration.regime import compute_regime
from app.tiers.concurrency import bounded_gather

log = get_logger(__name__)

_MAJORS = ["SPY", "QQQ", "IWM"]


async def _symbol_levels(symbol: str, now: datetime) -> tuple[str, IntradayLevels | None, float | None]:
    """Compute one symbol's intraday levels + intraday % change. Best-effort:
    a provider failure yields (symbol, None, None) rather than killing the sweep."""
    try:
        market = registry.market_data_provider()
        intraday = registry.intraday_provider()
        bars = await intraday.get_intraday_bars(symbol, interval="1min")
        quote = await market.get_quote(symbol)
        avg_vol = None
        try:
            hist = await market.get_price_history(symbol, lookback_days=20)
            vols = [c.volume for c in hist.candles if c.volume]
            avg_vol = sum(vols) / len(vols) if vols else None
        except Exception:  # noqa: BLE001 - avg volume is optional context
            avg_vol = None
        from app.shortduration.levels import rth_bars
        from app.shortduration.volume_profile import relative_volume_now
        rv_reading = await relative_volume_now(
            symbol, rth_bars(bars), now=now, avg_daily_volume=avg_vol
        )
        levels = compute_intraday_levels(
            symbol, bars, avg_daily_volume=avg_vol, relative_volume_reading=rv_reading,
            now=now, source=intraday.name
        )
        change_pct = None
        if quote.prev_close and quote.prev_close > 0:
            change_pct = round((quote.price - quote.prev_close) / quote.prev_close * 100, 3)
        return symbol, levels, change_pct
    except Exception as exc:  # noqa: BLE001 - one bad symbol must not kill the sweep
        log.warning("sd_symbol_levels_failed", symbol=symbol, error=str(exc))
        return symbol, None, None


async def _vol_reading() -> float | None:
    """A market volatility reading in [0,1] — SPY IV rank when available."""
    try:
        iv = await registry.options_chain_provider().get_iv_context("SPY")
        return iv.iv_rank
    except Exception as exc:  # noqa: BLE001
        log.warning("sd_vol_reading_failed", error=str(exc))
        return None


async def _next_event(now: datetime) -> EconomicEvent | None:
    try:
        events = await registry.econ_calendar_provider().get_economic_events(
            from_date=now.date()
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("sd_events_failed", error=str(exc))
        return None
    upcoming = [e for e in events if e.scheduled_at >= now]
    return min(upcoming, key=lambda e: e.scheduled_at) if upcoming else None


async def _market_internals(now: datetime):
    """Real market internals (best-effort). None if unavailable — the regime then
    falls back to the watchlist-participation proxy with capped confidence."""
    try:
        return await registry.market_internals_provider().get_market_internals(now=now)
    except Exception as exc:  # noqa: BLE001
        log.warning("sd_internals_failed", error=str(exc))
        return None


async def build_market_regime(
    *, now: datetime | None = None, universe: list[str] | None = None
) -> tuple[ShortDurationRegimeState, dict[str, IntradayLevels], WatchlistParticipation, MarketInternals | None]:
    now = now or datetime.now(UTC)
    syms = list(dict.fromkeys(_MAJORS + (universe or DEFAULT_UNIVERSE)))
    results = await bounded_gather([_symbol_levels(s, now) for s in syms], limit=8)
    levels: dict[str, IntradayLevels] = {}
    change: dict[str, float | None] = {}
    for res in results:
        if res is None:
            continue
        sym, lv, chg = res
        if lv is not None:
            levels[sym] = lv
        change[sym] = chg
    participation = compute_participation(list(levels.values()))
    vol_reading, next_event, internals = await asyncio.gather(
        _vol_reading(), _next_event(now), _market_internals(now)
    )
    regime = compute_regime(
        index_change_pct=change,
        index_levels=levels,
        participation=participation,
        internals=internals,
        vol_reading=vol_reading,
        next_event=next_event,
        now=now,
    )
    return regime, levels, participation, internals


async def get_symbol_levels(symbol: str, *, now: datetime | None = None) -> IntradayLevels | None:
    now = now or datetime.now(UTC)
    _, levels, _ = await _symbol_levels(symbol.upper(), now)
    return levels


async def get_news(
    symbols: list[str] | None = None, *, limit: int = 50
) -> list[NewsItem]:
    try:
        return await registry.news_provider().get_news(symbols, limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("sd_news_failed", error=str(exc))
        return []


async def get_events(*, now: datetime | None = None) -> list[EconomicEvent]:
    now = now or datetime.now(UTC)
    try:
        return await registry.econ_calendar_provider().get_economic_events(
            from_date=now.date()
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("sd_events_failed", error=str(exc))
        return []
