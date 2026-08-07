"""Persist a scan result, and read it back.

One function writes everything from a run in a single transaction, so a stored
run is either complete or absent. A half-written run is worse than none: it
would look like a run in which the agents found fewer candidates.

Nothing here writes a decision, an execution or a result. Those come from a
human, through `record_decision` / `record_execution` / `record_result`, and the
system never fabricates them.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_config import get_logger
from app.multiagent.db.models import (
    MAAgentRunRow,
    MACandidateRow,
    MADataQualityFlagRow,
    MADecisionRow,
    MAEconomicEventRow,
    MAExecutionRow,
    MAMarketBriefRow,
    MANewsItemRow,
    MAOptionContractSnapshotRow,
    MAOptionsFlowSnapshotRow,
    MAProviderRequestRow,
    MARecommendationRow,
    MAResultRow,
    MARunRow,
    MAScoreComponentRow,
    MAStockCatalystRow,
    MATechnicalSnapshotRow,
    MAValidationRow,
)
from app.multiagent.models.enums import DecisionAction
from app.multiagent.models.report import (
    RankedReport,
    TradeDecision,
    TradeExecution,
    TradeResult,
)

log = get_logger(__name__)


def _json(model) -> dict:
    return model.model_dump(mode="json")


def persist_scan(session: Session, result) -> str:
    """Write a whole `ScanResult`. Returns the run id.

    The caller owns the transaction boundary. `save_scan` below is the
    convenience wrapper that opens one.
    """
    report: RankedReport = result.report
    run = result.run
    rid = report.run_id

    session.add(
        MARunRow(
            run_id=rid,
            started_at=run.started_at,
            finished_at=run.finished_at,
            stage=run.stage.value,
            status=run.status.value,
            methodology_version=run.methodology_version,
            scoring_model_version=run.scoring_model_version,
            agent_runner=run.agent_runner,
            trading_mode=run.trading_mode,
            execution_enabled=run.execution_enabled,
            contracts_finalised=report.contracts_finalised,
            stage_note=report.stage_note,
            payload={"diagnostics": _json(report.diagnostics), "notes": run.notes},
        )
    )

    brief = report.brief
    session.add(
        MAMarketBriefRow(
            run_id=rid,
            generated_at=brief.generated_at,
            market_regime=brief.market_regime.value,
            volatility_regime=brief.volatility_regime.value,
            spy_bias=brief.spy_bias.value,
            qqq_bias=brief.qqq_bias.value,
            vix_level=brief.vix.level,
            relevance_confidence=brief.relevance_confidence,
            summary=brief.summary,
            payload=_json(brief),
        )
    )

    for event in list(brief.macro_events) + list(brief.upcoming_scheduled_events):
        session.add(
            MAEconomicEventRow(
                run_id=rid,
                evidence_id=(event.evidence_refs[0] if event.evidence_refs else None),
                name=event.name,
                catalyst_type=event.catalyst_type.value,
                scheduled_at=event.scheduled_at,
                importance=event.importance.value,
                consensus=event.consensus,
                previous=event.previous,
                actual=event.actual,
                payload=_json(event),
            )
        )

    for news in brief.news_items:
        session.add(
            MANewsItemRow(
                run_id=rid,
                evidence_id=news.evidence_id,
                symbol=news.ticker,
                headline=news.headline,
                source=news.source,
                url=news.url,
                published_at=news.published_at,
                retrieved_at=news.retrieved_at,
                catalyst_type=news.catalyst_type.value,
                scope=news.scope.value,
                evidence_quality="reported",
                relevance_confidence=news.relevance_confidence,
                payload=_json(news),
            )
        )

    for cat in brief.company_catalysts:
        session.add(
            MAStockCatalystRow(
                run_id=rid,
                ticker=cat.ticker,
                catalyst_type=cat.catalyst_type.value,
                headline=cat.headline,
                source=cat.source,
                source_url=cat.source_url,
                published_at=cat.published_at,
                expected_direction=cat.expected_direction.value,
                importance=cat.importance.value,
                importance_score=cat.importance_score,
                expected_time_horizon=cat.expected_time_horizon.value,
                scheduled_event_date=cat.scheduled_event_date,
                evidence_quality=cat.evidence_quality.value,
                scope=cat.scope.value,
                evidence_refs=list(cat.evidence_refs),
                payload=_json(cat),
            )
        )

    for candidate in result.candidates:
        session.add(
            MACandidateRow(
                candidate_id=candidate.candidate_id,
                run_id=rid,
                generated_at=candidate.generated_at,
                ticker=candidate.ticker,
                direction=candidate.direction.value,
                strategy_type=candidate.strategy_type.value,
                thesis=candidate.thesis,
                primary_catalyst=candidate.primary_catalyst,
                expected_holding_period=candidate.expected_holding_period.value,
                expected_move_pct=candidate.expected_move.magnitude_pct,
                underlying_reference_price=candidate.underlying_reference_price,
                invalidation_thesis=candidate.invalidation_thesis,
                earnings_date=candidate.earnings_date,
                catalyst_date=candidate.catalyst_date,
                preliminary_quality=candidate.preliminary_quality.value,
                payload=_json(candidate),
            )
        )

    for cid, report_v in result.validations.items():
        session.add(
            MAValidationRow(
                run_id=rid,
                candidate_id=cid,
                ticker=report_v.ticker,
                validated_at=report_v.validated_at,
                stage=report_v.stage,
                overall_verdict=report_v.overall_verdict.value,
                catalyst_verdict=(report_v.catalyst.verdict.value if report_v.catalyst else None),
                flow_verdict=(report_v.flow.verdict.value if report_v.flow else None),
                selected_structure_id=report_v.selected_structure_id,
                agent_commentary=report_v.agent_commentary,
                payload=_json(report_v),
            )
        )
        if report_v.technical:
            t = report_v.technical
            session.add(
                MATechnicalSnapshotRow(
                    run_id=rid,
                    candidate_id=cid,
                    symbol=t.symbol,
                    as_of=t.as_of,
                    price=t.price,
                    trend_bias=t.trend_bias.value,
                    bars_available=t.bars_available,
                    measurements=t.measurements.export(),
                    payload=_json(t),
                )
            )
        if report_v.flow:
            f = report_v.flow
            session.add(
                MAOptionsFlowSnapshotRow(
                    run_id=rid,
                    candidate_id=cid,
                    symbol=f.symbol,
                    as_of=f.as_of,
                    alerts_considered=f.alerts_considered,
                    call_premium=f.call_premium,
                    put_premium=f.put_premium,
                    net_premium=f.net_premium,
                    ask_side_premium=f.ask_side_premium,
                    sweep_count=f.sweep_count,
                    implied_bias=f.implied_bias.value,
                    direction_ambiguous=f.direction_ambiguous,
                    verdict=f.verdict.value,
                    payload=_json(f),
                )
            )
        for structure in report_v.structures:
            session.add(
                MAOptionContractSnapshotRow(
                    structure_id=structure.structure_id,
                    run_id=rid,
                    candidate_id=cid,
                    ticker=structure.ticker,
                    strategy_type=structure.strategy_type.value,
                    selected_at=structure.selected_at,
                    expiration=structure.expiration,
                    underlying_price=structure.underlying_price,
                    net_debit_per_share=structure.net_debit_per_share,
                    contracts=structure.contracts,
                    max_loss=structure.total_max_loss,
                    max_profit=structure.total_max_profit,
                    breakeven=structure.breakeven,
                    reward_to_risk=structure.reward_to_risk,
                    worst_leg_spread_pct=structure.worst_leg_spread_pct,
                    min_open_interest=structure.min_open_interest,
                    min_volume=structure.min_volume,
                    net_delta=structure.net_delta,
                    net_theta=structure.net_theta,
                    net_vega=structure.net_vega,
                    greeks_source=structure.greeks_source.value,
                    probability_of_profit=structure.probability_of_profit,
                    cost_drag_pct=structure.cost_drag_pct,
                    payload=_json(structure),
                )
            )

    # Recommendations: ranked AND rejected. The rejected rows are the point of
    # storing anything at all — see docs/multiagent/ARCHITECTURE.md.
    ranked_by_id = {t.candidate.candidate_id: t for t in report.ranked}
    for trade in report.ranked:
        _add_reco(session, rid, trade.candidate, trade.score, trade.classification.value,
                  is_ranked=True, rank=trade.rank, hard=False, codes=[], reasons=[],
                  structure_id=trade.validation.selected_structure_id)
        _add_components(session, rid, trade.candidate.candidate_id, trade.score)

    for rejected in report.rejected:
        if rejected.candidate.candidate_id in ranked_by_id or rejected.score is None:
            if rejected.score is None:
                # A candidate that failed before scoring still gets a row so the
                # run's candidate count and recommendation count reconcile.
                session.add(
                    MARecommendationRow(
                        run_id=rid,
                        candidate_id=rejected.candidate.candidate_id,
                        ticker=rejected.candidate.ticker,
                        strategy_type=rejected.candidate.strategy_type.value,
                        scored_at=report.generated_at,
                        methodology_version=report.methodology_version,
                        score=0.0,
                        raw_points=0.0,
                        measured_weight=0.0,
                        input_coverage=0.0,
                        classification="REJECT",
                        calibration_status=report.calibration_status.value,
                        is_ranked=False,
                        hard_rejected=True,
                        rejection_codes=list(rejected.rejection_codes),
                        rejection_reasons=list(rejected.rejection_reasons),
                        payload={"note": "validation failed before scoring"},
                    )
                )
            continue
        _add_reco(
            session, rid, rejected.candidate, rejected.score, "REJECT",
            is_ranked=False, rank=None, hard=rejected.hard_rejected,
            codes=list(rejected.rejection_codes), reasons=list(rejected.rejection_reasons),
            structure_id=(rejected.validation.selected_structure_id if rejected.validation else None),
        )
        _add_components(session, rid, rejected.candidate.candidate_id, rejected.score)

    for agent_run in run.agent_runs:
        session.add(
            MAAgentRunRow(
                run_id=rid,
                agent=agent_run.agent.value,
                stage=agent_run.stage.value,
                started_at=agent_run.started_at,
                finished_at=agent_run.finished_at,
                duration_ms=agent_run.duration_ms,
                status=agent_run.status.value,
                runner=agent_run.runner,
                definition_path=agent_run.definition_path,
                prompt_excerpt=agent_run.prompt_excerpt,
                raw_response_excerpt=agent_run.raw_response_excerpt,
                structured_output=agent_run.structured_output,
                tools_used=list(agent_run.tools_used),
                providers_queried=list(agent_run.providers_queried),
                errors=list(agent_run.errors),
                missing_data=list(agent_run.missing_data),
                validation_warnings=list(agent_run.validation_warnings),
                dropped_claims=list(agent_run.dropped_claims),
                input_tokens=agent_run.input_tokens,
                output_tokens=agent_run.output_tokens,
            )
        )

    for req in run.provider_requests:
        session.add(
            MAProviderRequestRow(
                run_id=rid,
                provider=req.provider,
                capability=req.capability,
                symbol=req.symbol,
                started_at=req.started_at,
                duration_ms=req.duration_ms,
                ok=req.ok,
                error=req.error,
                result_count=req.result_count,
                cache_hit=req.cache_hit,
            )
        )

    for flag in run.data_quality:
        session.add(
            MADataQualityFlagRow(
                run_id=rid,
                flag=flag.flag.value,
                subject=flag.subject[:255],
                detail=flag.detail,
                observed_at=flag.observed_at,
            )
        )

    log.info(
        "multiagent_run_persisted",
        run_id=rid,
        candidates=len(result.candidates),
        ranked=len(report.ranked),
        rejected=len(report.rejected),
    )
    return rid


def _add_reco(
    session: Session,
    run_id: str,
    candidate,
    score,
    classification: str,
    *,
    is_ranked: bool,
    rank: int | None,
    hard: bool,
    codes: list[str],
    reasons: list[str],
    structure_id: str | None,
) -> None:
    session.add(
        MARecommendationRow(
            run_id=run_id,
            candidate_id=candidate.candidate_id,
            ticker=candidate.ticker,
            strategy_type=candidate.strategy_type.value,
            scored_at=score.scored_at,
            methodology_version=score.methodology_version,
            score=score.score,
            raw_points=score.raw_points,
            measured_weight=score.measured_weight,
            input_coverage=score.input_coverage,
            classification=classification,
            calibration_status=score.calibration_status.value,
            is_ranked=is_ranked,
            rank=rank,
            hard_rejected=hard,
            rejection_codes=codes,
            rejection_reasons=reasons,
            structure_id=structure_id,
            payload={"breakdown": score.breakdown(), "audit": score.audit_lines()},
        )
    )


def _add_components(session: Session, run_id: str, candidate_id: str, score) -> None:
    for comp in score.components:
        session.add(
            MAScoreComponentRow(
                run_id=run_id,
                candidate_id=candidate_id,
                category=comp.category,
                weight=comp.weight,
                points_awarded=comp.points_awarded,
                points_available=comp.points_available,
                normalized=comp.normalized,
                abstained=comp.abstained,
                coverage=comp.coverage,
                rules=[_json(r) for r in comp.rules],
            )
        )


def save_scan(result) -> str:
    """Open a session, persist, commit."""
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        rid = persist_scan(session, result)
        session.commit()
        return rid


# --- human decision tracking -------------------------------------------------


def record_decision(
    session: Session,
    *,
    run_id: str,
    candidate_id: str,
    action: DecisionAction,
    decided_at: datetime,
    notes: str = "",
) -> TradeDecision:
    decision = TradeDecision(
        decision_id=str(uuid.uuid4()),
        run_id=run_id,
        candidate_id=candidate_id,
        action=action,
        decided_at=decided_at,
        notes=notes,
    )
    session.add(
        MADecisionRow(
            decision_id=decision.decision_id,
            run_id=run_id,
            candidate_id=candidate_id,
            action=action.value,
            decided_at=decided_at,
            notes=notes,
        )
    )
    return decision


def record_execution(session: Session, execution: TradeExecution) -> None:
    session.add(
        MAExecutionRow(
            execution_id=execution.execution_id,
            decision_id=execution.decision_id,
            candidate_id=execution.candidate_id,
            entered_at=execution.entered_at,
            contract_description=execution.contract_description,
            quantity=execution.quantity,
            entry_price_per_contract=execution.entry_price_per_contract,
            underlying_price_at_entry=execution.underlying_price_at_entry,
            stop_or_invalidation=execution.stop_or_invalidation,
            target=execution.target,
            notes=execution.notes,
        )
    )


def record_result(session: Session, result: TradeResult) -> None:
    session.add(
        MAResultRow(
            result_id=result.result_id,
            execution_id=result.execution_id,
            candidate_id=result.candidate_id,
            exited_at=result.exited_at,
            exit_price_per_contract=result.exit_price_per_contract,
            realized_pnl=result.realized_pnl,
            max_favorable_excursion_bound=result.max_favorable_excursion_bound,
            max_adverse_excursion_bound=result.max_adverse_excursion_bound,
            underlying_price_at_exit=result.underlying_price_at_exit,
            notes=result.notes,
        )
    )


# --- reads -------------------------------------------------------------------


def latest_runs(session: Session, limit: int = 10) -> Sequence[MARunRow]:
    return session.scalars(
        select(MARunRow).order_by(MARunRow.started_at.desc()).limit(limit)
    ).all()


def recommendations_for_run(session: Session, run_id: str) -> Sequence[MARecommendationRow]:
    return session.scalars(
        select(MARecommendationRow)
        .where(MARecommendationRow.run_id == run_id)
        .order_by(MARecommendationRow.score.desc())
    ).all()


def score_components_for(session: Session, candidate_id: str) -> Sequence[MAScoreComponentRow]:
    return session.scalars(
        select(MAScoreComponentRow).where(MAScoreComponentRow.candidate_id == candidate_id)
    ).all()
