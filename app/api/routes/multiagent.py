"""Multi-agent research endpoints: run a scan, read it back, record a decision.

**No execution endpoint exists here, and none is planned for this milestone.**
The platform's live-order path is `modes/execution_guard.py`, which is off by
default; this router does not reach it and exposes nothing that could.

The decision endpoints are the point of the router. Without a way to record
what the human did, the corpus can never answer "do trades scoring 80+ actually
outperform 70-79?" — and that question is the entire reason for storing anything.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.multiagent.db import (
    latest_runs,
    persist_scan,
    recommendations_for_run,
    record_decision,
    record_execution,
    record_result,
    score_components_for,
)
from app.multiagent.models.enums import DecisionAction, PipelineStage
from app.multiagent.models.report import RankedReport, TradeExecution, TradeResult
from app.multiagent.orchestrator import run_scan as run_multiagent_scan
from app.multiagent.reports import render_report

router = APIRouter(prefix="/multiagent", tags=["multiagent"])


class ScanRequest(BaseModel):
    stage: PipelineStage = PipelineStage.FULL
    symbols: list[str] | None = None
    persist: bool = True


class ScanSummary(BaseModel):
    run_id: str
    stage: str
    contracts_finalised: bool
    ranked: int
    rejected: int
    calibration_status: str
    methodology_version: str
    agent_runner: str


class RunListItem(BaseModel):
    run_id: str
    started_at: datetime
    stage: str
    status: str
    agent_runner: str
    methodology_version: str
    contracts_finalised: bool


class RecommendationSummary(BaseModel):
    candidate_id: str
    ticker: str
    strategy_type: str
    score: float
    input_coverage: float
    classification: str
    calibration_status: str
    is_ranked: bool
    rank: int | None
    hard_rejected: bool
    rejection_reasons: list[str]


class DecisionRequest(BaseModel):
    run_id: str
    candidate_id: str
    action: DecisionAction
    notes: str = ""


class DecisionResponse(BaseModel):
    decision_id: str
    action: str
    recorded_at: datetime


class ExecutionRequest(BaseModel):
    """A trade the HUMAN entered. The system never places one.

    Recorded so a recommendation can be tied to an outcome later.
    """

    decision_id: str
    candidate_id: str
    contract_description: str
    quantity: int = Field(gt=0)
    entry_price_per_contract: float
    underlying_price_at_entry: float | None = None
    entered_at: datetime | None = None
    stop_or_invalidation: str = ""
    target: str = ""
    notes: str = ""


class ResultRequest(BaseModel):
    execution_id: str
    candidate_id: str
    exited_at: datetime | None = None
    exit_price_per_contract: float | None = None
    realized_pnl: float | None = None
    # Named `_bound` because MFE/MAE come from bar extremes with no ordering
    # inside the bar. They are not achieved prices (CLAUDE.md §4).
    max_favorable_excursion_bound: float | None = None
    max_adverse_excursion_bound: float | None = None
    underlying_price_at_exit: float | None = None
    notes: str = ""


@router.post("/scans", response_model=ScanSummary)
async def create_scan(req: ScanRequest) -> ScanSummary:
    """Run one scan. Research only — this never places an order."""
    result = await run_multiagent_scan(stage=req.stage, symbols=req.symbols)
    if req.persist:
        from app.db.session import SessionLocal

        def _save() -> None:
            with SessionLocal() as session:
                persist_scan(session, result)
                session.commit()

        await run_in_threadpool(_save)

    report = result.report
    return ScanSummary(
        run_id=report.run_id,
        stage=report.stage.value,
        contracts_finalised=report.contracts_finalised,
        ranked=len(report.ranked),
        rejected=len(report.rejected),
        calibration_status=report.calibration_status.value,
        methodology_version=report.methodology_version,
        agent_runner=report.diagnostics.agent_runner,
    )


@router.post("/scans/report", response_model=RankedReport)
async def create_scan_full_report(req: ScanRequest) -> RankedReport:
    """Run a scan and return the whole report, score audit included."""
    result = await run_multiagent_scan(stage=req.stage, symbols=req.symbols)
    if req.persist:
        from app.db.session import SessionLocal

        def _save() -> None:
            with SessionLocal() as session:
                persist_scan(session, result)
                session.commit()

        await run_in_threadpool(_save)
    return result.report


@router.post("/scans/text")
async def create_scan_text(req: ScanRequest, audit: bool = Query(default=True)) -> dict:
    """The rendered console report, for a terminal or a paste."""
    result = await run_multiagent_scan(stage=req.stage, symbols=req.symbols)
    return {"run_id": result.report.run_id, "report": render_report(result.report, show_audit=audit)}


@router.get("/runs", response_model=list[RunListItem])
async def list_runs(limit: int = Query(default=20, ge=1, le=200)) -> list[RunListItem]:
    from app.db.session import SessionLocal

    def _read() -> list[RunListItem]:
        with SessionLocal() as session:
            return [
                RunListItem(
                    run_id=r.run_id,
                    started_at=r.started_at,
                    stage=r.stage,
                    status=r.status,
                    agent_runner=r.agent_runner,
                    methodology_version=r.methodology_version,
                    contracts_finalised=r.contracts_finalised,
                )
                for r in latest_runs(session, limit)
            ]

    return await run_in_threadpool(_read)


@router.get("/runs/{run_id}/recommendations", response_model=list[RecommendationSummary])
async def get_recommendations(run_id: str) -> list[RecommendationSummary]:
    """Ranked AND rejected. The rejected rows are half the value of the corpus."""
    from app.db.session import SessionLocal

    def _read() -> list[RecommendationSummary]:
        with SessionLocal() as session:
            rows = recommendations_for_run(session, run_id)
            if not rows:
                raise HTTPException(status_code=404, detail=f"no recommendations for run {run_id}")
            return [
                RecommendationSummary(
                    candidate_id=r.candidate_id,
                    ticker=r.ticker,
                    strategy_type=r.strategy_type,
                    score=r.score,
                    input_coverage=r.input_coverage,
                    classification=r.classification,
                    calibration_status=r.calibration_status,
                    is_ranked=r.is_ranked,
                    rank=r.rank,
                    hard_rejected=r.hard_rejected,
                    rejection_reasons=list(r.rejection_reasons or []),
                )
                for r in rows
            ]

    return await run_in_threadpool(_read)


@router.get("/candidates/{candidate_id}/audit")
async def get_score_audit(candidate_id: str) -> dict:
    """Every point, traced to the measurement that produced it."""
    from app.db.session import SessionLocal

    def _read() -> dict:
        with SessionLocal() as session:
            components = score_components_for(session, candidate_id)
            if not components:
                raise HTTPException(status_code=404, detail=f"no score for {candidate_id}")
            return {
                "candidate_id": candidate_id,
                "components": [
                    {
                        "category": c.category,
                        "weight": c.weight,
                        "points_awarded": c.points_awarded,
                        "points_available": c.points_available,
                        "normalized": c.normalized,
                        "abstained": c.abstained,
                        "coverage": c.coverage,
                        "rules": c.rules,
                    }
                    for c in components
                ],
            }

    return await run_in_threadpool(_read)


@router.post("/decisions", response_model=DecisionResponse)
async def create_decision(req: DecisionRequest) -> DecisionResponse:
    """Record the human's call: approved, rejected, watched, entered or skipped."""
    from app.db.session import SessionLocal

    now = datetime.now(UTC)

    def _write() -> DecisionResponse:
        with SessionLocal() as session:
            decision = record_decision(
                session,
                run_id=req.run_id,
                candidate_id=req.candidate_id,
                action=req.action,
                decided_at=now,
                notes=req.notes,
            )
            session.commit()
            return DecisionResponse(
                decision_id=decision.decision_id, action=req.action.value, recorded_at=now
            )

    return await run_in_threadpool(_write)


@router.post("/executions")
async def create_execution(req: ExecutionRequest) -> dict:
    """Record a trade the human entered manually.

    This endpoint does NOT place an order. It writes down what a person already
    did, so the recommendation and the outcome can be joined.
    """
    from app.db.session import SessionLocal

    execution = TradeExecution(
        execution_id=str(uuid.uuid4()),
        decision_id=req.decision_id,
        candidate_id=req.candidate_id,
        entered_at=req.entered_at or datetime.now(UTC),
        contract_description=req.contract_description,
        quantity=req.quantity,
        entry_price_per_contract=req.entry_price_per_contract,
        underlying_price_at_entry=req.underlying_price_at_entry,
        stop_or_invalidation=req.stop_or_invalidation,
        target=req.target,
        notes=req.notes,
    )

    def _write() -> dict:
        with SessionLocal() as session:
            record_execution(session, execution)
            session.commit()
            return {
                "execution_id": execution.execution_id,
                "recorded": True,
                "note": "Recorded only. This system does not place orders.",
            }

    return await run_in_threadpool(_write)


@router.post("/results")
async def create_result(req: ResultRequest) -> dict:
    from app.db.session import SessionLocal

    result = TradeResult(
        result_id=str(uuid.uuid4()),
        execution_id=req.execution_id,
        candidate_id=req.candidate_id,
        exited_at=req.exited_at,
        exit_price_per_contract=req.exit_price_per_contract,
        realized_pnl=req.realized_pnl,
        max_favorable_excursion_bound=req.max_favorable_excursion_bound,
        max_adverse_excursion_bound=req.max_adverse_excursion_bound,
        underlying_price_at_exit=req.underlying_price_at_exit,
        notes=req.notes,
    )

    def _write() -> dict:
        with SessionLocal() as session:
            record_result(session, result)
            session.commit()
            return {"result_id": result.result_id, "excursion_note": result.excursion_note}

    return await run_in_threadpool(_write)


@router.get("/methodology")
async def get_methodology_config() -> dict:
    """The live methodology, so a score can always be traced to its rubric."""
    from app.multiagent.config import get_methodology

    cfg = get_methodology()
    return {
        "version": cfg.version,
        "source": cfg.source_path,
        "weights": cfg.scoring.weights.model_dump(),
        "classification_bands": [b.model_dump() for b in cfg.classification.bands],
        "hard_rules": cfg.hard_rules.model_dump(),
        "allowed_strategies": cfg.strategies.allowed,
        "contracts": cfg.contracts.model_dump(),
        "note": (
            "Scores computed under this methodology display UNCALIBRATED until a feature "
            "clears out-of-sample validation. See docs/PRODUCT_STANCE.md."
        ),
    }
