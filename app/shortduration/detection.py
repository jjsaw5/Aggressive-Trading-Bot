"""Short-duration detection engine (Phase 2).

Builds a per-symbol `SetupContext` from the providers, runs the enabled strategy
modules for a DTE category, and turns confirmed setups into
`ShortDurationCandidate`s (persisted, with a state transition). This replaces the
Phase-1 context-scan stub: candidates now correspond to REAL, setup-first
detections — but still non-executing (no contract, no order).

Setup-first: a candidate exists only because a strategy confirmed a market setup.
The regime can annotate that new trades are currently blocked, but detection
itself is research output and is recorded either way.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from app.config import settings
from app.domain.enums import CandidateState, DTECategory
from app.domain.shortduration import (
    CandidateTransition,
    ContractRecommendation,
    ShortDurationCandidate,
    ShortDurationRegimeState,
)
from app.engine.universe import DEFAULT_UNIVERSE
from app.logging_config import get_logger
from app.providers import registry
from app.providers.ratelimit import Priority, use_priority
from app.shortduration.levels import compute_intraday_levels
from app.shortduration.service import build_market_regime
from app.shortduration.strategies.base import SetupContext, StrategyDetection
from app.shortduration.strategies.catalyst_continuation import CatalystContinuation
from app.shortduration.strategies.orb import OpeningRangeBreakout
from app.shortduration.strategies.trend_continuation import TrendContinuation
from app.shortduration.strategies.vwap_continuation import VWAPTrendContinuation
from app.tiers.concurrency import bounded_gather

log = get_logger(__name__)


def default_strategies(dte: DTECategory) -> list:
    """The strategy modules active for a DTE category (Phase 2 subset)."""
    if dte == DTECategory.ZERO_DTE:
        return [OpeningRangeBreakout(), VWAPTrendContinuation()]
    return [TrendContinuation(), CatalystContinuation()]


async def build_context(
    symbol: str, regime: ShortDurationRegimeState, now: datetime, dte: DTECategory
) -> SetupContext:
    """Fetch only what the DTE category's strategies need. Best-effort — a
    provider miss leaves that field empty and detectors guard for it."""
    market = registry.market_data_provider()
    ctx = SetupContext(symbol=symbol, now=now, regime=regime)

    async def _safe(coro, label):
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001 - one feed miss must not kill detection
            log.warning("sd_ctx_fetch_failed", symbol=symbol, feed=label, error=str(exc))
            return None

    ctx.bars_1m = await _safe(
        registry.intraday_provider().get_intraday_bars(symbol, interval="1min"), "intraday"
    ) or []
    ctx.quote = await _safe(market.get_quote(symbol), "quote")
    if ctx.quote and ctx.quote.prev_close:
        ctx.change_pct = round(
            (ctx.quote.price - ctx.quote.prev_close) / ctx.quote.prev_close * 100, 3
        )

    if dte == DTECategory.SHORT_DTE:
        ctx.daily = await _safe(market.get_price_history(symbol, lookback_days=252), "daily")
        ctx.catalysts = await _safe(
            registry.calendar_provider().get_catalysts(symbol), "catalysts"
        ) or []
        ctx.news = await _safe(
            registry.news_provider().get_news([symbol], limit=10), "news"
        ) or []
    else:
        ctx.flow = await _safe(
            registry.options_flow_provider().get_flow_alerts(symbol, limit=50), "flow"
        ) or []

    avg_vol = None
    if ctx.daily and ctx.daily.candles:
        vols = [c.volume for c in ctx.daily.candles if c.volume]
        avg_vol = sum(vols) / len(vols) if vols else None
    ctx.levels = compute_intraday_levels(
        symbol, ctx.bars_1m, avg_daily_volume=avg_vol, now=now,
        opening_range_minutes=settings.short_duration_opening_range_minutes,
        source=registry.intraday_provider().name,
    )
    return ctx


def _candidate_from(det: StrategyDetection, symbol: str, now: datetime) -> ShortDurationCandidate:
    return ShortDurationCandidate(
        id=uuid.uuid4().hex[:12],
        symbol=symbol,
        dte_category=det.dte_category,
        strategy=det.strategy,
        direction=det.direction,
        detected_at=now,
        regime=None,
        score=det.setup_score,
        confidence=det.setup_score,
        entry_trigger=det.entry_trigger,
        invalidation=det.invalidation,
        targets=det.targets,
        contract=ContractRecommendation(
            description="Contract selection lands in Phase 4 (defined-risk, sized to policy)."
        ),
        state=CandidateState.DETECTED,
        reasons=det.reasons,
    )


async def _detect_symbol(
    symbol: str, regime: ShortDurationRegimeState, now: datetime, dte: DTECategory
) -> tuple[str, list[StrategyDetection]]:
    ctx = await build_context(symbol, regime, now, dte)
    out: list[StrategyDetection] = []
    for strat in default_strategies(dte):
        try:
            det = strat.detect(ctx)
        except Exception as exc:  # noqa: BLE001 - a bad detector can't kill the sweep
            log.warning("sd_detect_failed", symbol=symbol, strategy=strat.key.value, error=str(exc))
            det = None
        if det is not None:
            out.append(det)
    return symbol, out


async def run_detection(
    dte: DTECategory, *, now: datetime | None = None, universe: list[str] | None = None
) -> list[ShortDurationCandidate]:
    """Detect real setups across the universe for a DTE category and persist the
    resulting candidates. Open-position API priority is unaffected — this runs at
    CANDIDATES priority."""
    from app.db import repository

    now = now or datetime.now(UTC)
    syms = universe or list(DEFAULT_UNIVERSE)
    regime, _levels, _breadth = await build_market_regime(now=now, universe=syms)

    with use_priority(Priority.CANDIDATES):
        results = await bounded_gather(
            [_detect_symbol(s, regime, now, dte) for s in syms],
            limit=settings.tier_concurrency,
        )

    created: list[ShortDurationCandidate] = []
    for res in results:
        if not res:
            continue
        symbol, dets = res
        for det in dets:
            cand = _candidate_from(det, symbol, now)
            cand.regime = regime.regime
            await asyncio.to_thread(repository.save_short_duration_candidate, cand)
            await asyncio.to_thread(
                repository.append_candidate_transition,
                CandidateTransition(
                    candidate_id=cand.id, from_state=None, to_state=CandidateState.DETECTED,
                    at=now, trigger=f"detection:{det.strategy.value}", actor="system",
                    reason=det.reasons[0] if det.reasons else "Setup detected.",
                    score_at=cand.score,
                ),
            )
            created.append(cand)
    # Highest-conviction first for the boards.
    created.sort(key=lambda c: c.score, reverse=True)
    log.info("sd_detection", dte=dte.value, detected=len(created))
    return created
