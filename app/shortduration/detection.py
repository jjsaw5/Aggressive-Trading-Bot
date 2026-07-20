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
from datetime import UTC, date, datetime

from app.config import settings
from app.domain.enums import CandidateState, DTECategory
from app.domain.shortduration import (
    CandidateTransition,
    ContractRecommendation,
    NewsScore,
    ScoreCard,
    ShortDurationCandidate,
    ShortDurationRegimeState,
)
from app.engine.universe import short_duration_universe
from app.logging_config import get_logger
from app.providers import registry
from app.providers.ratelimit import Priority, use_priority
from app.shortduration.contracts import ContractResult, select_short_duration_contracts
from app.shortduration.levels import compute_intraday_levels
from app.shortduration.risk import (
    EntryGate,
    evaluate_entry_gates,
    short_duration_policy,
)
from app.shortduration.scoring.data_quality import quote_is_stale
from app.shortduration.scoring.engine import score_candidate
from app.shortduration.scoring.flow_decay import analyze_flow
from app.shortduration.scoring.news import best_news_score
from app.shortduration.service import build_market_regime
from app.shortduration.state import classify_initial_state, transition
from app.shortduration.strategies.base import SetupContext, StrategyDetection
from app.shortduration.strategies.catalyst_continuation import CatalystContinuation
from app.shortduration.strategies.orb import OpeningRangeBreakout
from app.shortduration.strategies.trend_continuation import TrendContinuation
from app.shortduration.strategies.vwap_continuation import VWAPTrendContinuation
from app.tiers.concurrency import bounded_gather

log = get_logger(__name__)


def _target_expirations(dte: DTECategory, today: date) -> list[date]:
    """The expiration dates a DTE category trades. Providers return contracts for
    whichever of these they actually list (liquid names have daily/weekly ones)."""
    from datetime import timedelta

    if dte == DTECategory.ZERO_DTE:
        offsets = range(0, 2)  # today (+ next day as a fallback if today isn't listed)
    else:
        offsets = range(1, settings.short_duration_max_dte + 1)
    return [today + timedelta(days=i) for i in offsets]


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

    # Flow is used by both categories — 0DTE flow-quality and 1-5DTE
    # multi-session-flow are both material factors in the scoring models.
    ctx.flow = await _safe(
        registry.options_flow_provider().get_flow_alerts(symbol, limit=50), "flow"
    ) or []
    if dte == DTECategory.SHORT_DTE:
        ctx.daily = await _safe(market.get_price_history(symbol, lookback_days=252), "daily")
        ctx.catalysts = await _safe(
            registry.calendar_provider().get_catalysts(symbol), "catalysts"
        ) or []
        ctx.news = await _safe(
            registry.news_provider().get_news([symbol], limit=10), "news"
        ) or []

    avg_vol = None
    if ctx.daily and ctx.daily.candles:
        vols = [c.volume for c in ctx.daily.candles if c.volume]
        avg_vol = sum(vols) / len(vols) if vols else None
    from app.shortduration.levels import rth_bars
    from app.shortduration.volume_profile import relative_volume_now
    rv_reading = await relative_volume_now(
        symbol, rth_bars(ctx.bars_1m), now=now, avg_daily_volume=avg_vol
    )
    ctx.levels = compute_intraday_levels(
        symbol, ctx.bars_1m, avg_daily_volume=avg_vol, relative_volume_reading=rv_reading, now=now,
        opening_range_minutes=settings.short_duration_opening_range_minutes,
        source=registry.intraday_provider().name,
    )
    return ctx


def _candidate_from(
    det: StrategyDetection, symbol: str, now: datetime,
    card: ScoreCard, news: NewsScore | None, regime: ShortDurationRegimeState,
    contract: ContractResult, gate: EntryGate,
) -> ShortDurationCandidate:
    reasons = list(det.reasons)
    reasons.append(card.summary)
    if contract.recommendation.description:
        reasons.append(f"Contract: {contract.recommendation.description}.")
    plan = contract.plan
    rr = plan.risk.reward_to_risk if plan and plan.risk else None
    return ShortDurationCandidate(
        id=uuid.uuid4().hex[:12],
        symbol=symbol,
        dte_category=det.dte_category,
        strategy=det.strategy,
        direction=det.direction,
        detected_at=now,
        regime=regime.regime,
        score=card.normalized,
        confidence=card.overall_confidence,
        entry_trigger=det.entry_trigger,
        invalidation=det.invalidation,
        targets=det.targets,
        contract=contract.recommendation,
        trade_plan=plan,
        max_risk_usd=plan.risk.max_loss_usd if plan else None,
        reward_to_risk=rr,
        state=CandidateState.DETECTED,
        data_quality_score=card.data_quality,
        reasons=reasons,
        scorecard=card,
        news_score=news,
        entry_allowed=gate.allowed,
        entry_notes=gate.reasons,
        reject_reasons=[r.value for r in contract.reject_reasons] + [r.value for r in gate.reject_reasons],
    )


async def _detect_symbol(
    symbol: str, regime: ShortDurationRegimeState, now: datetime, dte: DTECategory
) -> tuple[str, SetupContext, list[StrategyDetection]]:
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
    return symbol, ctx, out


async def _score_symbol(
    symbol: str, ctx: SetupContext, dets: list[StrategyDetection], now: datetime
) -> list[tuple[StrategyDetection, ScoreCard, NewsScore | None, ContractResult, EntryGate]]:
    """For a symbol that produced setups: fetch the chain + IV once, select a
    sized defined-risk contract per detection, score with the real structure, and
    evaluate the entry gates. Chain fetches happen ONLY for symbols with
    detections — a handful, not the whole universe."""
    chain = iv = None
    dte = dets[0].dte_category  # all detections in a scan share the DTE category
    try:
        # Fetch the NEAR-TERM expirations this category trades, not the default
        # ~30-DTE window get_option_chain returns.
        chain = await registry.options_chain_provider().get_option_chain_for_expirations(
            symbol, _target_expirations(dte, now.date())
        )
    except Exception as exc:  # noqa: BLE001 - liquidity scored as unknown on miss
        log.warning("sd_score_chain_failed", symbol=symbol, error=str(exc))
    try:
        iv = await registry.options_chain_provider().get_iv_context(symbol)
    except Exception as exc:  # noqa: BLE001
        log.warning("sd_score_iv_failed", symbol=symbol, error=str(exc))

    rel_vol = ctx.levels.relative_volume if ctx.levels else None
    stale = quote_is_stale(ctx, now)
    equity = settings.account_equity_usd
    # Real daily posture from today's closed paper trades feeds the loss/halt gates.
    from app.shortduration.paper import daily_risk_state
    daily = await asyncio.to_thread(daily_risk_state, now)
    scored = []
    for det in dets:
        fa = analyze_flow(ctx.flow, now, det.direction)
        news = best_news_score(
            ctx.news, for_symbol=symbol, change_pct=ctx.change_pct, rel_volume=rel_vol, flow=fa
        )
        # Offer EVERY viable defined-risk expression (long AND spread) as its own
        # candidate, so the board shows a mix to pick from. Each is scored on its
        # own structure. A rejected setup yields a single non-tradeable candidate.
        contracts: list[ContractResult] = [ContractResult(None, ContractRecommendation(description=""))]
        if chain is not None:
            contracts = select_short_duration_contracts(
                chain, det.direction, det.dte_category,
                policy=short_duration_policy(det.dte_category), as_of=now.date(),
            )
        gate = evaluate_entry_gates(
            dte=det.dte_category, direction=det.direction, regime=ctx.regime, now=now,
            quote_stale=stale, daily=daily, equity=equity,
        )
        for contract in contracts:
            card = score_candidate(
                ctx, det, chain=chain, iv=iv, news_score=news, flow_analysis=fa, trade_plan=contract.plan
            )
            scored.append((det, card, news, contract, gate))
    return scored


async def run_detection(
    dte: DTECategory, *, now: datetime | None = None, universe: list[str] | None = None
) -> list[ShortDurationCandidate]:
    """Detect real setups across the universe, SCORE them with the per-DTE model,
    classify their initial state, and persist candidates + the full transition
    trail. Runs at CANDIDATES priority so open-position monitoring keeps its
    precedence."""
    from app.db import repository

    now = now or datetime.now(UTC)
    syms = universe or short_duration_universe(dte == DTECategory.ZERO_DTE)
    regime, _levels, _breadth = await build_market_regime(now=now, universe=syms)

    with use_priority(Priority.CANDIDATES):
        detected = await bounded_gather(
            [_detect_symbol(s, regime, now, dte) for s in syms],
            limit=settings.tier_concurrency,
        )
        with_dets = [(s, ctx, dets) for r in detected if r for (s, ctx, dets) in [r] if dets]
        scored_rows = await bounded_gather(
            [_score_symbol(s, ctx, dets, now) for (s, ctx, dets) in with_dets],
            limit=settings.tier_concurrency,
        )

    created: list[ShortDurationCandidate] = []
    for (symbol, _ctx, _dets), scored in zip(with_dets, scored_rows, strict=False):
        if not scored:
            continue
        for det, card, news, contract, gate in scored:
            cand = _candidate_from(det, symbol, now, card, news, regime, contract, gate)
            transitions = _classify_transitions(cand, det, now, tradeable=contract.is_tradeable)
            await asyncio.to_thread(repository.save_short_duration_candidate, cand)
            for tr in transitions:
                await asyncio.to_thread(repository.append_candidate_transition, tr)
            created.append(cand)

    created.sort(key=lambda c: c.score, reverse=True)
    log.info("sd_detection", dte=dte.value, detected=len(created))
    return created


def _classify_transitions(
    cand: ShortDurationCandidate, det: StrategyDetection, now: datetime, *, tradeable: bool
) -> list[CandidateTransition]:
    """DETECTED -> EVALUATING, then either REJECTED (no tradeable defined-risk
    contract) or WATCHLIST/ARMED from the score."""
    trail = [
        CandidateTransition(
            candidate_id=cand.id, from_state=None, to_state=CandidateState.DETECTED, at=now,
            trigger=f"detection:{det.strategy.value}", actor="system",
            reason=det.reasons[0] if det.reasons else "Setup detected.", score_at=cand.score,
        )
    ]
    trail.append(
        transition(cand, CandidateState.EVALUATING, trigger="scored", actor="system",
                   reason=cand.scorecard.summary if cand.scorecard else "Scored.", at=now)
    )
    if not tradeable:
        # A valid setup with no liquid, defined-risk contract that fits the cap is
        # rejected — visibly, with the reason, not silently dropped.
        why = "; ".join(cand.reject_reasons) or "No tradeable defined-risk contract."
        trail.append(
            transition(cand, CandidateState.REJECTED, trigger="no_contract", actor="system",
                       reason=why, at=now)
        )
        return trail
    target = classify_initial_state(
        cand.score, watchlist_at=settings.short_duration_watchlist_score,
        arm_at=settings.short_duration_arm_score,
    )
    if target != CandidateState.EVALUATING:
        trail.append(
            transition(cand, target, trigger="score_threshold", actor="system",
                       reason=f"Score {cand.score:.2f} crossed the {target.value} threshold.", at=now)
        )
    return trail
