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
from app.shortduration.contracts import ContractResult, is_swing, select_short_duration_contracts
from app.shortduration.levels import compute_intraday_levels
from app.shortduration.risk import (
    EntryGate,
    evaluate_entry_gates,
    short_duration_policy,
)
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
from app.shortduration.thesis import build_directional_thesis
from app.tiers.concurrency import bounded_gather

log = get_logger(__name__)


async def _account_state(now: datetime):
    """Best-effort capital snapshot for sizing. On any provider error, returns None
    so sizing falls back to the configured equity (never blocks a scan)."""
    try:
        return await registry.account_state_provider().get_account_state(now=now)
    except Exception as exc:  # noqa: BLE001 - sizing degrades to the constant, never fails the scan
        log.warning("sd_account_state_failed", error=str(exc))
        return None


def _target_expirations(dte: DTECategory, today: date) -> list[date]:
    """The expiration dates a DTE category trades. Providers return contracts for
    whichever of these they actually list (liquid names have daily/weekly ones)."""
    from datetime import timedelta

    if dte == DTECategory.ZERO_DTE:
        offsets = range(0, 2)  # today (+ next day as a fallback if today isn't listed)
    else:
        offsets = range(1, settings.short_duration_max_dte + 1)
    return [today + timedelta(days=i) for i in offsets]


def _swing_expirations(today: date) -> list[date]:
    """The weeks-out window a swing (daily-trend) thesis is expressed in, so a
    TrendContinuation setup lands in a 20-45 DTE contract that matches its horizon."""
    from datetime import timedelta

    return [today + timedelta(days=i) for i in range(settings.swing_min_dte, settings.swing_max_dte + 1)]


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
    # Next earnings date drives the earnings-before-expiry guardrail (both tracks).
    _earn = await _safe(registry.calendar_provider().get_earnings(symbol), "earnings")
    ctx.next_earnings = _earn.report_date if _earn else None

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


def _candidate_odds(
    det: StrategyDetection, symbol: str, contract: ContractResult, spot: float | None,
    iv30: float | None, now: datetime,
) -> tuple[float | None, str]:
    """Market-implied P(profit) + a plain-English "what has to happen" line for a
    sized contract. Both informational — from the structure's break-even, IV, and
    days to expiry. Degrades to (None, "") when the structure or inputs are missing."""
    from app.domain.enums import Direction
    from app.quant.analytics import structure_breakevens
    from app.quant.probability import probability_of_profit, what_has_to_happen

    plan = contract.plan
    if plan is None or not spot or spot <= 0:
        return None, ""
    bes = structure_breakevens(plan)
    if not bes:
        return None, ""
    # One-directional debit structures have a single break-even; pick the one on
    # the profitable side (nearest is that one for our long/vertical shapes).
    breakeven = min(bes, key=lambda b: abs(b - spot))
    bullish = det.direction == Direction.BULLISH
    exps = [lg.expiration for lg in plan.legs]
    days = (min(exps) - now.date()).days if exps else None
    pop = None
    if iv30 and days is not None:
        pop = probability_of_profit(spot=spot, breakeven=breakeven, iv=iv30, days=float(days), bullish=bullish)
    what = what_has_to_happen(
        symbol=symbol, spot=spot, breakeven=breakeven, days=days, bullish=bullish,
    )
    return pop, what


def _apply_cost_drag(contract: ContractResult, chain, as_of: date) -> None:
    """Attach the structure's round-trip spread cost-drag to the recommendation
    (Layer-1 cost-drag rank). Cost-drag = the bid/ask spread you forfeit getting in
    AND out at the quote, as a fraction of the structure's own defined max-loss. It's
    the tax that makes a wider expression rank below a tighter one at similar merit.
    Best-effort: leaves the field None when the chain lacks two-sided quotes."""
    plan = contract.plan
    if plan is None or chain is None or not plan.legs:
        return
    by_key = {
        (round(c.strike, 4), c.option_type, c.expiration): c for c in chain.contracts
    }
    one_way_per_share = 0.0
    covered = 0
    for lg in plan.legs:
        c = by_key.get((round(lg.strike, 4), lg.option_type, lg.expiration))
        if c is None or c.bid is None or c.ask is None or c.ask <= c.bid:
            continue
        one_way_per_share += (c.ask - c.bid) / 2.0
        covered += 1
    if covered < len(plan.legs) or one_way_per_share <= 0:
        return  # can't price every leg's spread → don't fabricate a drag number
    round_trip_usd = one_way_per_share * 2.0 * 100.0 * plan.contracts
    max_loss = plan.risk.max_loss_usd if plan.risk else None
    if not max_loss or max_loss <= 0:
        return
    ratio = round(round_trip_usd / max_loss, 4)
    contract.recommendation.cost_drag_ratio = ratio
    contract.recommendation.cost_drag_note = (
        f"Round-trip spread tax ≈ ${round_trip_usd:.0f} = {ratio * 100:.0f}% of "
        f"${max_loss:.0f} max risk."
    )


def _candidate_from(
    det: StrategyDetection, symbol: str, now: datetime,
    card: ScoreCard, news: NewsScore | None, regime: ShortDurationRegimeState,
    contract: ContractResult, gate: EntryGate, fresh=None, levels=None, thesis=None,
    pop=None, what_has="",
) -> ShortDurationCandidate:
    from app.shortduration.exit_plan import build_short_duration_exit_plan

    reasons = list(det.reasons)
    reasons.append(card.summary)
    if contract.recommendation.description:
        reasons.append(f"Contract: {contract.recommendation.description}.")
    plan = contract.plan
    rr = plan.risk.reward_to_risk if plan and plan.risk else None
    exit_plan = build_short_duration_exit_plan(det, levels=levels, plan=plan)
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
        exit_plan=exit_plan,
        thesis=thesis,
        probability_of_profit=pop,
        what_has_to_happen=what_has,
        max_risk_usd=plan.risk.max_loss_usd if plan else None,
        reward_to_risk=rr,
        state=CandidateState.DETECTED,
        data_quality_score=card.data_quality,
        reasons=reasons,
        scorecard=card,
        news_score=news,
        scoring_model_version=card.model_version,
        risk_policy_version=card.risk_policy_version,
        signal_metadata=dict(det.metadata) if det.metadata else {},
        entry_allowed=gate.allowed,
        entry_notes=gate.reasons,
        freshness=fresh.model_dump() if fresh is not None else None,
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
    symbol: str, ctx: SetupContext, dets: list[StrategyDetection], now: datetime, account=None
) -> list[tuple]:
    """For a symbol that produced setups: fetch the chain + IV once, select a
    sized defined-risk contract per detection, score with the real structure, and
    evaluate the entry gates. Chain fetches happen ONLY for symbols with
    detections — a handful, not the whole universe."""
    chain = iv = None
    dte = dets[0].dte_category  # all detections in a scan share the DTE category
    # A swing (daily-trend) detection is expressed weeks out, so also fetch that
    # expiry window when one is present — the near-term set alone can't hold it.
    expirations = _target_expirations(dte, now.date())
    if any(is_swing(d.strategy) for d in dets):
        expirations = expirations + _swing_expirations(now.date())
    try:
        # Fetch the NEAR-TERM expirations this category trades, not the default
        # ~30-DTE window get_option_chain returns.
        chain = await registry.options_chain_provider().get_option_chain_for_expirations(
            symbol, expirations
        )
    except Exception as exc:  # noqa: BLE001 - liquidity scored as unknown on miss
        log.warning("sd_score_chain_failed", symbol=symbol, error=str(exc))
    current_iv = None
    try:
        current_iv = await registry.options_chain_provider().get_iv_context(symbol)
    except Exception as exc:  # noqa: BLE001
        log.warning("sd_score_iv_failed", symbol=symbol, error=str(exc))
    # The chain provider returns only the spot IV level (iv30); IV RANK/PERCENTILE —
    # what the volatility factor, data-quality, and POP all need — are computed from a
    # real IV history (or an HV proxy). The short-duration scan previously skipped this
    # join, so iv_rank was always None: it blanked the volatility factor, capped data
    # quality, and made POP uncomputable. Reuse the SAME builder the funnel pipeline
    # uses so rank is consistent app-wide.
    iv_hist = None
    ivp = registry.iv_history_provider()
    if ivp is not None:
        try:
            iv_hist = await ivp.get_iv_history(symbol, lookback_days=365)
        except Exception as exc:  # noqa: BLE001 - rank degrades to HV proxy / unknown
            log.warning("sd_score_iv_history_failed", symbol=symbol, error=str(exc))
    if current_iv is not None or iv_hist is not None or ctx.daily is not None:
        from app.engine.iv_context import build_iv_context
        iv = build_iv_context(
            symbol,
            current_iv.iv30 if current_iv is not None else None,
            now,
            iv_history=iv_hist,
            price_history=ctx.daily,
            term_structure_slope=current_iv.term_structure_slope if current_iv is not None else None,
        )

    # Enrich the IV context with a basic put/call skew read from the chain we just
    # fetched (the provider's get_iv_context has no chain to compute it from).
    if iv is not None and chain is not None and chain.underlying_price:
        from app.quant.iv import put_call_iv_skew
        iv.iv_skew = put_call_iv_skew(chain, chain.underlying_price)

    rel_vol = ctx.levels.relative_volume if ctx.levels else None
    # State/track-aware freshness: a trade-ready 0DTE name needs a seconds-fresh
    # quote, not the 120s broad-screen budget. Evaluate at the trade-ready
    # (armed) budget for 0DTE, watchlist budget otherwise.
    from app.domain.enums import CandidateState
    from app.shortduration.freshness import evaluate_quote_freshness
    fresh = evaluate_quote_freshness(
        as_of=ctx.quote.as_of if ctx.quote else None,
        delayed_minutes=ctx.quote.delayed_minutes if ctx.quote else None,
        now=now, capability="underlying",
        state=CandidateState.ARMED if dte == DTECategory.ZERO_DTE else CandidateState.WATCHLIST,
        dte=dte, provider=ctx.quote.source if ctx.quote else None,
    )
    stale = not fresh.ok
    # Sizing reads the real capital picture (equity minus committed risk), not a
    # bare constant. Falls back to configured equity if no account state was passed.
    equity = account.equity_usd if account is not None else settings.account_equity_usd
    open_risk = account.committed_risk_usd if account is not None else 0.0
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
            from app.shortduration.strategies.base import dominant_flow_strike
            contracts = select_short_duration_contracts(
                chain, det.direction, det.dte_category,
                policy=short_duration_policy(det.dte_category, equity=equity),
                as_of=now.date(), open_risk_usd=open_risk, swing=is_swing(det.strategy),
                prefer_strike=dominant_flow_strike(ctx.flow, det.direction),
            )
        gate = evaluate_entry_gates(
            dte=det.dte_category, direction=det.direction, regime=ctx.regime, now=now,
            quote_stale=stale, daily=daily, equity=equity, symbol=symbol,
        )
        thesis = build_directional_thesis(ctx, det, news_score=news)
        spot = chain.underlying_price if chain is not None else (ctx.quote.price if ctx.quote else None)
        iv30 = iv.iv30 if iv is not None else None
        for contract in contracts:
            card = score_candidate(
                ctx, det, chain=chain, iv=iv, news_score=news, flow_analysis=fa, trade_plan=contract.plan
            )
            _apply_cost_drag(contract, chain, now.date())
            pop, what_has = _candidate_odds(det, symbol, contract, spot, iv30, now)
            scored.append((det, card, news, contract, gate, fresh, thesis, pop, what_has))
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
    regime, _levels, _part, _internals = await build_market_regime(now=now, universe=syms)
    # One capital snapshot for the whole scan: sizing draws on real equity minus
    # committed (open + pending) risk, subject to buying power. Best-effort — a
    # provider miss falls back to the configured constant inside _score_symbol.
    account = await _account_state(now)

    with use_priority(Priority.CANDIDATES):
        detected = await bounded_gather(
            [_detect_symbol(s, regime, now, dte) for s in syms],
            limit=settings.tier_concurrency,
        )
        with_dets = [(s, ctx, dets) for r in detected if r for (s, ctx, dets) in [r] if dets]
        scored_rows = await bounded_gather(
            [_score_symbol(s, ctx, dets, now, account) for (s, ctx, dets) in with_dets],
            limit=settings.tier_concurrency,
        )

    created: list[ShortDurationCandidate] = []
    for (symbol, _ctx, _dets), scored in zip(with_dets, scored_rows, strict=False):
        if not scored:
            continue
        for det, card, news, contract, gate, fresh, thesis, pop, what_has in scored:
            cand = _candidate_from(
                det, symbol, now, card, news, regime, contract, gate, fresh,
                levels=_ctx.levels, thesis=thesis, pop=pop, what_has=what_has,
            )
            transitions = _classify_transitions(cand, det, now, tradeable=contract.is_tradeable)
            await asyncio.to_thread(repository.save_short_duration_candidate, cand)
            for tr in transitions:
                await asyncio.to_thread(repository.append_candidate_transition, tr)
            created.append(cand)

    created.sort(key=_board_rank_key)
    _record_scan_metrics(dte, created)
    log.info("sd_detection", dte=dte.value, detected=len(created))
    return created


def _board_rank_key(c: ShortDurationCandidate) -> tuple:
    """Layer-1 board order: by score bucket first (highest merit on top), then by
    real spread cost-drag within the bucket so the tightest-to-trade expression of a
    near-equal setup ranks above a wider one that merely scored a hair higher. Missing
    cost-drag sorts last within its bucket (unknown tax is not treated as cheap)."""
    bucket = settings.board_rank_score_bucket or 0.05
    score_bucket = round(c.score / bucket)  # coarse merit band
    drag = c.contract.cost_drag_ratio if c.contract else None
    drag_key = drag if drag is not None else float("inf")
    return (-score_bucket, drag_key, -c.score)


def _record_scan_metrics(dte: DTECategory, created: list[ShortDurationCandidate]) -> None:
    """Observability counters for a completed scan (candidates, stale-blocked,
    tradeable). Never raises — metrics must not affect a scan."""
    try:
        from app.observability.metrics import get_metrics

        m = get_metrics()
        tag = dte.value
        stale = sum(1 for c in created if c.freshness and not c.freshness.get("ok", True))
        tradeable = sum(1 for c in created if c.trade_plan is not None)
        m.inc(f"sd.scan.candidates.{tag}", len(created))
        m.inc(f"sd.scan.stale_blocked.{tag}", stale)
        m.inc(f"sd.scan.tradeable.{tag}", tradeable)
        m.set_gauge(f"sd.scan.last_candidates.{tag}", float(len(created)))
    except Exception as exc:  # noqa: BLE001
        log.warning("sd_scan_metrics_failed", error=str(exc))


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
    # Layer-1 arming discipline: the hand-weighted rank is not calibrated conviction,
    # so ARM (the "go" tier) is withheld unless the rank is least misleading — POP must
    # be computable, and 0DTE never asserts conviction unless explicitly enabled. A
    # blocked-but-arm-worthy candidate is held at WATCHLIST with the reason recorded.
    sc = cand.scorecard
    pop_ok = bool(sc.pop_available) if sc is not None else False
    zero_dte = det.dte_category == DTECategory.ZERO_DTE
    arm_blocked_reason = ""
    if not pop_ok:
        arm_blocked_reason = "POP uncomputable (no IV) — held at watchlist, not armed."
    elif zero_dte and not settings.zero_dte_conviction:
        arm_blocked_reason = "0DTE asserts no calibrated conviction — held at watchlist, not armed."
    allow_arm = not arm_blocked_reason
    target = classify_initial_state(
        cand.score, watchlist_at=settings.short_duration_watchlist_score,
        arm_at=settings.short_duration_arm_score, allow_arm=allow_arm,
    )
    if not allow_arm and cand.score >= settings.short_duration_arm_score:
        # The score cleared the arm bar but arming was withheld — make that visible
        # on the candidate, not just in the transition audit trail.
        cand.reasons.append(f"Arm withheld: {arm_blocked_reason}")
    if target != CandidateState.EVALUATING:
        reason = f"Score {cand.score:.2f} crossed the {target.value} threshold."
        if not allow_arm and cand.score >= settings.short_duration_arm_score:
            reason = f"Score {cand.score:.2f} is arm-worthy but {arm_blocked_reason}"
        trail.append(
            transition(cand, target, trigger="score_threshold", actor="system",
                       reason=reason, at=now)
        )
    return trail
