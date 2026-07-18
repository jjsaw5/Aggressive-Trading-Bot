"""Short-duration orchestration service (read-only, Phase 1).

Fetches data through the provider registry, computes intraday levels + breadth +
regime, and pulls news/events. Kept out of the API layer so routes stay thin and
the logic is unit-testable with a mock provider. Concurrency is bounded and open
positions are not touched here (Tier 4 owns those).

Phase 1 has NO strategy detection: `run_context_scan` produces context-only
candidates that capture the current market posture so the boards, persistence,
and state machine are exercised. Real detection replaces the scoring in Phase 2 —
these candidates are explicitly labeled non-actionable.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from app.domain.enums import CandidateState, Direction, DTECategory
from app.domain.shortduration import (
    CandidateTransition,
    EconomicEvent,
    IntradayLevels,
    NewsItem,
    ShortDurationCandidate,
    ShortDurationRegimeState,
)
from app.engine.universe import DEFAULT_UNIVERSE
from app.logging_config import get_logger
from app.providers import registry
from app.shortduration.breadth import BreadthProxy, compute_breadth
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
        levels = compute_intraday_levels(
            symbol, bars, avg_daily_volume=avg_vol, now=now, source=intraday.name
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


async def build_market_regime(
    *, now: datetime | None = None, universe: list[str] | None = None
) -> tuple[ShortDurationRegimeState, dict[str, IntradayLevels], BreadthProxy]:
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
    breadth = compute_breadth(list(levels.values()))
    vol_reading, next_event = await asyncio.gather(_vol_reading(), _next_event(now))
    regime = compute_regime(
        index_change_pct=change,
        index_levels=levels,
        breadth=breadth,
        vol_reading=vol_reading,
        next_event=next_event,
        now=now,
    )
    return regime, levels, breadth


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


def _context_candidate(
    symbol: str, dte: DTECategory, levels: IntradayLevels, regime: ShortDurationRegimeState, now: datetime
) -> ShortDurationCandidate:
    """A NON-actionable context snapshot (Phase 1). Direction reflects current
    posture vs VWAP; score stays 0 because no setup has been confirmed yet."""
    above = levels.above_vwap
    direction = (
        Direction.BULLISH if above else Direction.BEARISH if above is False else Direction.NEUTRAL
    )
    reasons = ["Context snapshot only — strategy detection lands in Phase 2."]
    if levels.vwap is not None and levels.last is not None:
        reasons.append(f"{'Above' if above else 'Below'} VWAP ({levels.last:g} vs {levels.vwap:g}).")
    if levels.relative_volume is not None:
        reasons.append(f"Relative volume ~{levels.relative_volume:g}x.")
    return ShortDurationCandidate(
        id=uuid.uuid4().hex[:12],
        symbol=symbol,
        dte_category=dte,
        direction=direction,
        detected_at=now,
        regime=regime.regime,
        score=0.0,
        confidence=0.0,
        state=CandidateState.DETECTED,
        reasons=reasons,
    )


async def run_context_scan(
    dte: DTECategory, *, now: datetime | None = None, universe: list[str] | None = None
) -> list[ShortDurationCandidate]:
    """Phase-1 scan stub: snapshot market context for each universe symbol into
    context-only candidates (persisted, with a state transition). This exercises
    the boards + state machine; Phase 2 replaces it with real detection."""
    from app.db import repository

    now = now or datetime.now(UTC)
    regime, levels, _ = await build_market_regime(now=now, universe=universe)
    out: list[ShortDurationCandidate] = []
    for sym in universe or DEFAULT_UNIVERSE:
        lv = levels.get(sym)
        if lv is None:
            continue
        cand = _context_candidate(sym, dte, lv, regime, now)
        await asyncio.to_thread(repository.save_short_duration_candidate, cand)
        await asyncio.to_thread(
            repository.append_candidate_transition,
            CandidateTransition(
                candidate_id=cand.id,
                from_state=None,
                to_state=CandidateState.DETECTED,
                at=now,
                trigger="context_scan",
                actor="system",
                reason="Context snapshot created.",
                score_at=0.0,
            ),
        )
        out.append(cand)
    log.info("sd_context_scan", dte=dte.value, count=len(out))
    return out
