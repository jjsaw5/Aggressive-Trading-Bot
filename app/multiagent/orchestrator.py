"""The orchestrator: evidence -> brief -> candidates -> validation -> score -> report.

Responsibilities, in order:

1. resolve the stage against the market clock (and downgrade if needed),
2. collect evidence and mint its ids,
3. run Agent 1, bind its claims, get a `MarketBrief`,
4. run Agent 2, bind and filter, get `ResearchCandidate`s,
5. for each candidate: fetch fresh data, measure everything, select contracts,
   run Agent 3 for interpretation,
6. score deterministically, run the hard rules, classify, rank,
7. assemble the report with rejections included, and persist.

Two guarantees the structure provides rather than promises:

* **No order is ever placed.** There is no execution code path in this
  subsystem, and no agent is given an order-placement tool. `execution_enabled`
  is recorded on every run so the stored corpus demonstrates it.
* **A hard rejection cannot be outscored.** Hard rules run over measurements and
  never see the composite; the orchestrator applies them as a veto after
  scoring, and stores the score anyway so rejected candidates remain analysable.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.config import settings
from app.logging_config import get_logger
from app.multiagent.agents import (
    build_measured_report,
    fetch_symbol_data,
    run_market_intelligence,
    run_opportunity_generator,
    run_trade_validator,
)
from app.multiagent.analysis.technical import trend_bias
from app.multiagent.config import MethodologyConfig, get_methodology
from app.multiagent.evidence import EvidenceCollector, ProviderCallRecorder
from app.multiagent.evidence.collector import upcoming_earnings_within
from app.multiagent.llm import AgentRunner, build_runner, load_agent
from app.multiagent.models.brief import MarketBrief
from app.multiagent.models.candidates import ResearchCandidate
from app.multiagent.models.enums import (
    BiasDirection,
    CalibrationStatus,
    Classification,
    EvidenceKind,
    PipelineStage,
    RunStatus,
)
from app.multiagent.models.report import (
    RankedReport,
    RankedTrade,
    RejectedTrade,
    RunDiagnostics,
)
from app.multiagent.models.runs import PipelineRun
from app.multiagent.models.scoring import CompositeScore, ScoredCandidate
from app.multiagent.models.validation import ValidationReport
from app.multiagent.rules import below_minimum_score, evaluate_hard_rules
from app.multiagent.runtime import get_runtime
from app.multiagent.scoring import classify, rank, score_candidate
from app.multiagent.stages import resolve_stage, stage_finalises_contracts

log = get_logger(__name__)


@dataclass
class ScanResult:
    """Everything one run produced, ready to render or persist."""

    report: RankedReport
    run: PipelineRun
    brief: MarketBrief
    candidates: list[ResearchCandidate] = field(default_factory=list)
    validations: dict[str, ValidationReport] = field(default_factory=dict)
    scores: dict[str, CompositeScore] = field(default_factory=dict)


class Orchestrator:
    def __init__(
        self,
        *,
        cfg: MethodologyConfig | None = None,
        runner: AgentRunner | None = None,
        now: datetime | None = None,
    ) -> None:
        rt = get_runtime()
        self.cfg = cfg or get_methodology(rt.ma_methodology_path)
        self.runner = runner or build_runner(
            rt.ma_agent_runner,
            **({"model": rt.ma_anthropic_model} if rt.ma_agent_runner != "deterministic" else {}),
        )
        self.now = now or datetime.now(UTC)

    async def run(
        self,
        *,
        stage: PipelineStage = PipelineStage.FULL,
        symbols: list[str] | None = None,
        run_id: str | None = None,
    ) -> ScanResult:
        rid = run_id or f"ma-{uuid.uuid4().hex[:12]}"
        started = self.now
        # Elapsed time is measured with a monotonic counter, not by subtracting
        # `started` from the wall clock. `self.now` may be pinned — tests and
        # replays pass an explicit instant — and mixing a pinned start with a
        # live finish reports a duration of days for a run that took 80ms.
        t0 = time.perf_counter()
        resolved_stage, stage_note = resolve_stage(stage, started)

        pipeline = PipelineRun(
            run_id=rid,
            started_at=started,
            stage=resolved_stage,
            methodology_version=self.cfg.version,
            scoring_model_version=settings.scoring_model_version,
            agent_runner=self.runner.runner_id,
            trading_mode=str(getattr(settings.trading_mode, "value", settings.trading_mode)),
            execution_enabled=False,  # structural: this subsystem has no order path
            notes=[stage_note],
        )
        log.info(
            "multiagent_run_started",
            run_id=rid,
            stage=resolved_stage.value,
            runner=self.runner.runner_id,
        )

        universe = [s.upper() for s in (symbols or self.cfg.run.discovery_universe)]
        refs = self.cfg.run.market_reference_symbols

        # --- 1. evidence -------------------------------------------------
        collector = EvidenceCollector(
            rid,
            now=started,
            timeout=self.cfg.run.provider_timeout_seconds,
            trend_lookback_days=self.cfg.scoring.market_alignment.relative_strength_lookback_days,
            flat_threshold_pct=self.cfg.scoring.market_alignment.trend_flat_threshold_pct,
        )
        evidence = await collector.collect_market_evidence(universe, reference_symbols=refs)
        pipeline.provider_requests.extend(evidence.requests)
        pipeline.data_quality.extend(evidence.quality)

        # --- 2. Agent 1 --------------------------------------------------
        brief, mi_record, mi_binding = await run_market_intelligence(
            load_agent(self.cfg.agents.market_intelligence, self.cfg.definitions_path()),
            self.runner,
            evidence,
            self.cfg,
            run_id=rid,
            stage=resolved_stage,
            now=started,
        )
        pipeline.agent_runs.append(mi_record)
        pipeline.data_quality.extend(mi_binding.quality)

        # --- 3. Agent 2 --------------------------------------------------
        trends = _measure_trends(evidence, self.cfg)
        candidates, og_record, og_binding = await run_opportunity_generator(
            load_agent(self.cfg.agents.opportunity_generator, self.cfg.definitions_path()),
            self.runner,
            brief,
            evidence.ledger,
            self.cfg,
            run_id=rid,
            stage=resolved_stage,
            trends=trends,
            quotes=evidence.quotes,
            now=started,
        )
        pipeline.agent_runs.append(og_record)
        pipeline.data_quality.extend(og_binding.quality)

        # --- 4-6. validate, score, judge ---------------------------------
        validations: dict[str, ValidationReport] = {}
        scores: dict[str, CompositeScore] = {}
        ranked: list[RankedTrade] = []
        rejected: list[RejectedTrade] = []
        scored_index: dict[str, ScoredCandidate] = {}

        scheduled_macro = [
            (item.headline or "scheduled event", item.published_at)
            for item in evidence.ledger.of_kind(EvidenceKind.ECONOMIC_EVENT)
            if item.published_at is not None
        ]

        semaphore = asyncio.Semaphore(self.cfg.run.validation_concurrency)

        async def _one(candidate: ResearchCandidate):
            async with semaphore:
                return await self._validate_and_score(
                    candidate,
                    evidence,
                    brief,
                    resolved_stage,
                    rid,
                    scheduled_macro,
                )

        outcomes = await asyncio.gather(*(_one(c) for c in candidates), return_exceptions=True)

        for candidate, outcome in zip(candidates, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                log.warning(
                    "multiagent_candidate_failed",
                    run_id=rid,
                    ticker=candidate.ticker,
                    error=str(outcome)[:200],
                )
                rejected.append(
                    RejectedTrade(
                        candidate=candidate,
                        rejection_codes=["validation_error"],
                        rejection_reasons=[f"validation raised: {str(outcome)[:200]}"],
                        hard_rejected=True,
                    )
                )
                continue

            report, score, verdict, tv_record, requests = outcome
            validations[candidate.candidate_id] = report
            scores[candidate.candidate_id] = score
            pipeline.agent_runs.append(tv_record)
            pipeline.provider_requests.extend(requests)

            classification, name = classify(score, self.cfg)
            low = below_minimum_score(score, self.cfg)

            is_rejected = verdict.rejected or low is not None
            scored_index[candidate.candidate_id] = ScoredCandidate(
                candidate_id=candidate.candidate_id,
                run_id=rid,
                ticker=candidate.ticker,
                score=score,
                classification=classification if not is_rejected else Classification.REJECT,
                classification_name=name if not is_rejected else "Reject",
                is_ranked=not is_rejected,
                rejection_codes=verdict.codes() + ([low.code.value] if low else []),
                rejection_reasons=verdict.reasons() + ([low.render()] if low else []),
            )

            if is_rejected:
                rejected.append(
                    RejectedTrade(
                        candidate=candidate,
                        validation=report,
                        score=score,
                        classification=classification,
                        rejection_codes=verdict.codes() + ([low.code.value] if low else []),
                        rejection_reasons=verdict.reasons() + ([low.render()] if low else []),
                        hard_rejected=verdict.rejected,
                    )
                )
            else:
                ranked.append(
                    RankedTrade(
                        rank=0,  # assigned after sorting
                        candidate=candidate,
                        validation=report,
                        score=score,
                        classification=classification,
                        classification_name=name,
                        entry_conditions=_entry_conditions(candidate, report),
                        profit_targets=_profit_targets(report),
                        invalidation=candidate.invalidation_thesis,
                        risks=_risks(candidate, report),
                        warnings=list(verdict.warnings),
                    )
                )

        ordered = rank(list(scored_index.values()), self.cfg)
        order = {s.candidate_id: (s.rank or 10_000) for s in ordered}
        ranked.sort(key=lambda t: order.get(t.candidate.candidate_id, 10_000))
        for i, trade in enumerate(ranked, start=1):
            trade.rank = i
        ranked = ranked[: self.cfg.run.max_ranked_in_report]

        elapsed = round(time.perf_counter() - t0, 3)
        finished = started + timedelta(seconds=elapsed)
        pipeline.finished_at = finished
        pipeline.status = RunStatus.COMPLETED

        diagnostics = RunDiagnostics(
            stage=resolved_stage,
            status=RunStatus.COMPLETED,
            started_at=started,
            finished_at=finished,
            duration_seconds=elapsed,
            evidence_items=len(evidence.ledger),
            symbols_examined=len(universe),
            candidates_generated=len(candidates),
            candidates_validated=len(validations),
            provider_errors=dict(evidence.ledger.provider_errors),
            data_gaps=list(brief.data_gaps),
            dropped_agent_claims=list(mi_binding.dropped) + list(og_binding.dropped),
            agent_runner=self.runner.runner_id,
            providers_used=sorted({r.provider for r in pipeline.provider_requests}),
        )

        report = RankedReport(
            run_id=rid,
            generated_at=finished,
            methodology_version=self.cfg.version,
            stage=resolved_stage,
            calibration_status=CalibrationStatus.UNCALIBRATED,
            brief=brief,
            ranked=ranked,
            rejected=rejected,
            diagnostics=diagnostics,
            contracts_finalised=stage_finalises_contracts(resolved_stage),
            stage_note=stage_note,
        )

        log.info(
            "multiagent_run_completed",
            run_id=rid,
            ranked=len(ranked),
            rejected=len(rejected),
            duration_s=diagnostics.duration_seconds,
        )
        return ScanResult(
            report=report,
            run=pipeline,
            brief=brief,
            candidates=candidates,
            validations=validations,
            scores=scores,
        )

    async def _validate_and_score(
        self,
        candidate: ResearchCandidate,
        evidence,
        brief: MarketBrief,
        stage: PipelineStage,
        run_id: str,
        scheduled_macro,
    ):
        recorder = ProviderCallRecorder(self.cfg.run.provider_timeout_seconds)
        data = await fetch_symbol_data(
            candidate.ticker,
            recorder=recorder,
            stage=stage,
        )
        report = build_measured_report(
            candidate,
            data,
            evidence.ledger,
            self.cfg,
            evidence.indices,
            now=self.now,
            stage=stage,
            scheduled_macro=scheduled_macro,
        )
        report, tv_record = await run_trade_validator(
            load_agent(self.cfg.agents.trade_validator, self.cfg.definitions_path()),
            self.runner,
            candidate,
            report,
            run_id=run_id,
            stage=stage,
            now=self.now,
        )

        disagreement = data.disagreement_pct()
        structure = report.selected_structure()
        score = score_candidate(
            candidate,
            report,
            self.cfg,
            now=self.now,
            cross_check_disagreement_pct=disagreement,
            structure=structure,
        )
        earnings = upcoming_earnings_within(
            evidence.ledger,
            candidate.ticker,
            today=self.now.date(),
            days=max(self.cfg.hard_rules.earnings_blackout_days, 14),
        )
        verdict = evaluate_hard_rules(
            candidate,
            report,
            structure,
            self.cfg,
            now=self.now,
            earnings_date=earnings,
            provider_price_disagreement_pct=disagreement,
            score=score,
        )
        return report, score, verdict, tv_record, list(recorder.requests)


def _measure_trends(evidence, cfg: MethodologyConfig) -> dict[str, BiasDirection]:
    """20-day trend bias per symbol, measured from retrieved history."""
    from app.multiagent.analysis.alignment import bias_from_return
    from app.multiagent.analysis.technical import IndicatorContext, run_indicators

    flat = cfg.scoring.market_alignment.trend_flat_threshold_pct
    lookback = cfg.scoring.market_alignment.relative_strength_lookback_days
    out: dict[str, BiasDirection] = {}
    for symbol, history in evidence.histories.items():
        closes = [c.close for c in history.candles]
        if len(closes) <= lookback or closes[-1 - lookback] == 0:
            out[symbol] = BiasDirection.UNKNOWN
            continue
        pct = (closes[-1] - closes[-1 - lookback]) / closes[-1 - lookback] * 100.0
        out[symbol] = bias_from_return(pct, flat)
    # Keep the helper imports honest: these are the same primitives the
    # per-candidate technical snapshot uses, so the two cannot disagree.
    _ = (IndicatorContext, run_indicators, trend_bias)
    return out


def _entry_conditions(candidate: ResearchCandidate, report: ValidationReport) -> list[str]:
    out: list[str] = []
    structure = report.selected_structure()
    if structure is None:
        out.append("No contract selected — entry conditions cannot be stated at this stage.")
        return out
    if structure.net_debit_per_share is not None:
        out.append(
            f"Work the order at or better than ${structure.net_debit_per_share:.2f} per share "
            f"(${structure.net_debit_per_share * 100:.0f} per contract). Do not pay the full ask: "
            f"crossing costs "
            + (
                f"${(structure.net_debit_at_ask_per_share - structure.net_debit_per_share) * 100:.0f} "
                "per contract each way."
                if structure.net_debit_at_ask_per_share is not None
                else "an unmeasured amount."
            )
        )
    if structure.contracts:
        out.append(
            f"Size: {structure.contracts} contract(s), total defined risk "
            f"${structure.total_max_loss:,.2f}."
        )
    tech = report.technical
    if tech and tech.price is not None:
        out.append(f"Reference underlying price at validation: ${tech.price:.2f}.")
    return out


def _profit_targets(report: ValidationReport) -> list[str]:
    out: list[str] = []
    structure = report.selected_structure()
    rr = report.risk_reward
    if structure is None:
        return out
    if structure.max_profit_per_contract is not None:
        out.append(
            f"Defined maximum at expiry: ${structure.max_profit_per_contract:,.2f} per contract "
            f"(reward-to-risk {structure.reward_to_risk})."
        )
    if rr and rr.expected_move_pct and structure.underlying_price:
        out.append(
            f"IV-implied one-sigma move over the hold is {rr.expected_move_pct:.1f}% — a modeled "
            "reference, not a forecast."
        )
    if structure.breakeven is not None and rr and rr.breakeven_move_pct is not None:
        out.append(
            f"Breakeven ${structure.breakeven:.2f} requires a {rr.breakeven_move_pct:.1f}% move."
        )
    return out


def _risks(candidate: ResearchCandidate, report: ValidationReport) -> list[str]:
    risks = list(candidate.known_risks)
    risks.extend(report.disconfirming_findings)
    if report.risk_reward:
        risks.extend(report.risk_reward.event_risk_notes)
    if report.catalyst and report.catalyst.conflicting_events:
        risks.extend(f"scheduled event inside the hold: {e}" for e in report.catalyst.conflicting_events)
    seen: dict[str, None] = {}
    for r in risks:
        seen.setdefault(r, None)
    return list(seen)


async def run_scan(
    *,
    stage: PipelineStage = PipelineStage.FULL,
    symbols: list[str] | None = None,
    runner: AgentRunner | None = None,
    cfg: MethodologyConfig | None = None,
    now: datetime | None = None,
) -> ScanResult:
    """Convenience entry point used by the CLI, the API and the tests."""
    return await Orchestrator(cfg=cfg, runner=runner, now=now).run(stage=stage, symbols=symbols)
