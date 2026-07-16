"""Scan orchestration: universe -> per-symbol analysis -> ranked candidates.

This is the heart of Mode 1 (research). For each symbol it gathers data through
the provider abstraction, applies hard liquidity/quality gates, runs the signal
analyzers, composites a score, selects a contract, builds a defined-risk trade
plan, and checks account-level admission — producing a fully explained
`TradeCandidate` that answers all 14 platform questions.

Symbols that fail a hard gate are still returned (status=REJECTED) with the
reasons attached, so nothing is silently dropped — traceability first.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.domain.candidates import Thesis, TradeCandidate
from app.domain.enums import CandidateStatus, Direction, RejectReason
from app.domain.signals import SignalBundle
from app.engine.catalysts import analyze_catalysts
from app.engine.flow import analyze_flow
from app.engine.iv_context import build_iv_context
from app.engine.liquidity import gate_underlying
from app.engine.price_action import analyze_price_action
from app.engine.scoring import ScoreWeights, composite_score, resolve_direction
from app.engine.strategy_selector import build_best_plan
from app.engine.universe import UniverseConfig
from app.engine.volatility import analyze_volatility
from app.logging_config import get_logger
from app.providers.base import (
    CalendarProvider,
    FundamentalsProvider,
    IVHistoryProvider,
    MarketDataProvider,
    OptionsChainProvider,
    OptionsFlowProvider,
)
from app.quant.analytics import compute_analytics
from app.risk.exit_plan import for_trade_plan
from app.risk.policy import RiskPolicy
from app.risk.portfolio import PortfolioState, evaluate_admission

log = get_logger(__name__)


class ScanEngine:
    def __init__(
        self,
        *,
        market: MarketDataProvider,
        fundamentals: FundamentalsProvider,
        chain: OptionsChainProvider,
        flow: OptionsFlowProvider,
        calendar: CalendarProvider,
        iv_history: IVHistoryProvider | None = None,
        policy: RiskPolicy | None = None,
        universe: UniverseConfig | None = None,
        weights: ScoreWeights | None = None,
    ) -> None:
        self.market = market
        self.fundamentals = fundamentals
        self.chain = chain
        self.flow = flow
        self.calendar = calendar
        self.iv_history = iv_history
        self.policy = policy or RiskPolicy.from_settings()
        self.universe = universe or UniverseConfig()
        self.weights = weights or ScoreWeights()

    async def run(
        self, portfolio: PortfolioState | None = None
    ) -> list[TradeCandidate]:
        portfolio = portfolio or PortfolioState(positions=[])
        scan_id = uuid.uuid4().hex[:12]
        now = datetime.now(UTC)
        candidates: list[TradeCandidate] = []

        for symbol in self.universe.normalized_symbols():
            try:
                cand = await self._evaluate_symbol(symbol, scan_id, now, portfolio)
                if cand:
                    candidates.append(cand)
            except Exception as exc:  # one bad symbol must not kill the scan
                log.warning("symbol_eval_failed", symbol=symbol, error=str(exc))

        candidates.sort(key=lambda c: c.composite_score, reverse=True)
        log.info(
            "scan_complete",
            scan_id=scan_id,
            total=len(candidates),
            actionable=sum(c.is_actionable for c in candidates),
        )
        return candidates

    async def _evaluate_symbol(
        self,
        symbol: str,
        scan_id: str,
        now: datetime,
        portfolio: PortfolioState,
    ) -> TradeCandidate | None:
        quote = await self.market.get_quote(symbol)
        fundamentals = await self.fundamentals.get_fundamentals(symbol)

        # Hard underlying gate.
        underlying_rejects = gate_underlying(fundamentals, quote.price, self.universe)

        history = await self.market.get_price_history(symbol, lookback_days=252)

        # Current IV snapshot from the chain provider; IV RANK is computed from a
        # real IV history (or a realized-vol proxy) rather than trusted opaquely.
        current = await self.chain.get_iv_context(symbol)
        iv_hist = None
        if self.iv_history is not None:
            try:
                iv_hist = await self.iv_history.get_iv_history(symbol, lookback_days=365)
            except Exception as exc:
                log.warning("iv_history_failed", symbol=symbol, error=str(exc))
        iv = build_iv_context(
            symbol,
            current.iv30,
            now,
            iv_history=iv_hist,
            price_history=history,
            term_structure_slope=current.term_structure_slope,
        )
        flow_alerts = await self.flow.get_flow_alerts(symbol=symbol, unusual_only=True)
        catalysts = await self.calendar.get_catalysts(symbol)

        # Signals.
        flow_sig = analyze_flow(symbol, flow_alerts)
        price_sig = analyze_price_action(history)
        direction = resolve_direction(flow_sig, price_sig)
        vol_sig = analyze_volatility(iv, direction or Direction.NEUTRAL)
        cat_sig = analyze_catalysts(symbol, catalysts, now.date())

        bundle = SignalBundle(symbol=symbol, scores=[flow_sig, price_sig, vol_sig, cat_sig])
        score = composite_score(bundle, self.weights)

        reject_reasons: list[RejectReason] = list(underlying_rejects)
        iv_rank = iv.iv_rank
        has_catalyst = bool(cat_sig.details.get("has_catalyst"))

        thesis = Thesis(
            direction=direction,
            why_now=flow_sig.rationale,
            flow_meaningful=flow_sig.score >= 0.35,
            price_confirms=(price_sig.direction == direction and direction != Direction.NEUTRAL),
            has_catalyst=has_catalyst,
            catalyst_note=cat_sig.rationale,
            iv_favorable=vol_sig.score >= 0.5,
            iv_note=vol_sig.rationale,
            invalidation="See trade plan invalidation once a structure is selected.",
        )

        trade_plan = None
        final_direction = direction
        status = CandidateStatus.RANKED

        if underlying_rejects:
            status = CandidateStatus.REJECTED
        else:
            chain = await self.chain.get_option_chain(symbol)
            plan = build_best_plan(
                chain, direction, iv_rank, has_catalyst, self.policy, now.date(),
                open_risk_usd=portfolio.open_risk_usd,
            )
            if plan is None:
                reject_reasons.append(
                    RejectReason.WEAK_SIGNAL
                    if direction == Direction.NEUTRAL
                    else RejectReason.NO_VALID_CONTRACT
                    if not chain.contracts
                    else RejectReason.RISK_UNMANAGEABLE
                )
                status = CandidateStatus.REJECTED
            else:
                admission = evaluate_admission(
                    plan.risk.max_loss_usd, portfolio, self.policy
                )
                if not admission.admitted:
                    reject_reasons.extend(admission.reasons)
                    status = CandidateStatus.REJECTED
                else:
                    # Attach structure analytics (POP, net greeks, breakevens)
                    # and the mechanical exit plan (take-profit / stop / time-stop
                    # net prices).
                    vol_for_pricing = iv.iv30 or 0.4
                    plan.analytics = compute_analytics(
                        plan, quote.price, vol_for_pricing, now.date()
                    )
                    plan.exit_plan = for_trade_plan(plan)
                    trade_plan = plan
                    final_direction = plan.direction  # vol structures set VOL_*
                    thesis.direction = final_direction
                    thesis.invalidation = plan.risk.invalidation_note

        return TradeCandidate(
            symbol=symbol,
            status=status,
            composite_score=score,
            direction=final_direction,
            thesis=thesis,
            signals=bundle.scores,
            trade_plan=trade_plan,
            reject_reasons=reject_reasons,
            generated_at=now,
            scan_id=scan_id,
        )
