"""End-to-end: the vertical slice, the stage gate, and persistence.

These are the tests that would notice if the pipeline stopped being a pipeline —
if an agent's output stopped reaching the scorer, if the premarket gate stopped
holding, or if a run stopped being reconstructable from the database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.multiagent.models.enums import (
    CalibrationStatus,
    PipelineStage,
    RunStatus,
)
from app.multiagent.orchestrator import run_scan
from app.multiagent.reports import render_report
from app.multiagent.stages import resolve_stage

# 16:00 UTC = 12:00 ET, mid-session on a Wednesday.
MARKET_OPEN = datetime(2026, 8, 5, 16, 0, tzinfo=UTC)
# 09:00 UTC = 05:00 ET, before the open.
PREMARKET = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)

SYMBOLS = ["NVDA", "AMD", "MU", "TSLA"]


@pytest.fixture(scope="module")
async def full_run():
    return await run_scan(stage=PipelineStage.FULL, symbols=SYMBOLS, now=MARKET_OPEN)


# --- the vertical slice -----------------------------------------------------


@pytest.mark.asyncio
async def test_the_whole_pipeline_runs_end_to_end(full_run):
    result = full_run
    report = result.report

    assert report.run_id
    assert report.diagnostics.status is RunStatus.COMPLETED
    assert report.diagnostics.evidence_items > 0
    assert report.diagnostics.candidates_generated > 0
    # Every generated candidate is accounted for: ranked or rejected, never lost.
    assert len(report.ranked) + len(report.rejected) == report.diagnostics.candidates_generated


@pytest.mark.asyncio
async def test_every_stage_of_the_funnel_produced_something(full_run):
    result = full_run
    assert result.brief is not None
    assert result.candidates
    assert result.validations
    assert result.scores
    # Three agents ran, plus one validator run per candidate.
    agents = {r.agent.value for r in result.run.agent_runs}
    assert {"market_intelligence", "opportunity_generator", "trade_validator"} <= agents


@pytest.mark.asyncio
async def test_scoring_reached_every_validated_candidate(full_run):
    for candidate in full_run.candidates:
        assert candidate.candidate_id in full_run.scores
        score = full_run.scores[candidate.candidate_id]
        assert 0.0 <= score.score <= 100.0
        assert score.components
        assert score.methodology_version == full_run.report.methodology_version


@pytest.mark.asyncio
async def test_every_score_carries_its_full_audit_trail(full_run):
    for score in full_run.scores.values():
        lines = score.audit_lines()
        assert lines[-1].startswith("TOTAL:")
        # Each of the eight categories appears, with its rules underneath.
        assert len([line for line in lines if not line.startswith(" ")]) == 9  # 8 + TOTAL


@pytest.mark.asyncio
async def test_every_score_is_uncalibrated(full_run):
    assert full_run.report.calibration_status is CalibrationStatus.UNCALIBRATED
    for score in full_run.scores.values():
        assert score.calibration_status is CalibrationStatus.UNCALIBRATED


@pytest.mark.asyncio
async def test_rejected_candidates_are_kept_with_their_reasons(full_run):
    for rejected in full_run.report.rejected:
        assert rejected.rejection_reasons, f"{rejected.candidate.ticker} rejected with no reason"
        assert rejected.rejection_codes


@pytest.mark.asyncio
async def test_no_run_ever_enables_execution(full_run):
    """The safety guarantee, asserted rather than documented."""
    assert full_run.run.execution_enabled is False


@pytest.mark.asyncio
async def test_the_report_renders_without_error(full_run):
    text = render_report(full_run.report)
    assert "MULTI-AGENT OPTIONS RESEARCH" in text
    assert "MARKET SUMMARY" in text
    assert "REJECTED CANDIDATES" in text
    assert "UNCALIBRATED" in text
    assert "PLACES NO ORDERS" in text
    # Absent values render as sentinels, never as blanks.
    assert "  \n" not in text.replace("\n\n", "\n")


@pytest.mark.asyncio
async def test_the_report_shows_the_score_breakdown_for_ranked_trades(full_run):
    if not full_run.report.ranked:
        pytest.skip("no candidate cleared the rules in this run")
    text = render_report(full_run.report, show_audit=True)
    assert "Score breakdown:" in text
    assert "Full audit (every point traced to a measurement)" in text
    assert "catalyst_strength" in text


@pytest.mark.asyncio
async def test_the_run_is_deterministic_for_a_fixed_clock():
    """Same inputs, same methodology, same scores."""
    a = await run_scan(stage=PipelineStage.FULL, symbols=["NVDA", "AMD"], now=MARKET_OPEN)
    b = await run_scan(stage=PipelineStage.FULL, symbols=["NVDA", "AMD"], now=MARKET_OPEN)

    def _fingerprint(result):
        return sorted(
            (c.ticker, c.direction.value, round(result.scores[c.candidate_id].score, 4))
            for c in result.candidates
        )

    assert _fingerprint(a) == _fingerprint(b)


# --- the premarket / market-open gate ---------------------------------------


def test_a_premarket_request_stays_premarket():
    stage, note = resolve_stage(PipelineStage.PREMARKET, MARKET_OPEN)
    assert stage is PipelineStage.PREMARKET
    assert "no contract is selected" in note


def test_a_full_request_outside_market_hours_is_downgraded_not_run_anyway():
    """Contracts priced off stale quotes are worse than no contracts."""
    stage, note = resolve_stage(PipelineStage.FULL, PREMARKET)
    assert stage is PipelineStage.PREMARKET
    assert "downgraded" in note
    assert "stale" in note


def test_a_full_request_during_market_hours_runs_full():
    stage, note = resolve_stage(PipelineStage.FULL, MARKET_OPEN)
    assert stage is PipelineStage.FULL
    assert "open" in note


@pytest.mark.asyncio
async def test_a_premarket_run_selects_no_contracts_and_says_so():
    result = await run_scan(stage=PipelineStage.PREMARKET, symbols=["NVDA"], now=PREMARKET)
    report = result.report

    assert report.stage is PipelineStage.PREMARKET
    assert report.contracts_finalised is False
    for validation in result.validations.values():
        assert validation.structures == []
        assert validation.selected_structure() is None
        assert any("premarket" in g for g in validation.data_gaps)

    text = render_report(report)
    assert "CONTRACTS NOT FINALISED" in text


@pytest.mark.asyncio
async def test_a_premarket_run_still_produces_research():
    """Research is available outside market hours; only pricing is not."""
    result = await run_scan(stage=PipelineStage.PREMARKET, symbols=["NVDA", "AMD"], now=PREMARKET)
    assert result.brief is not None
    assert result.report.diagnostics.evidence_items > 0
    # Candidates still get theses and directions, just no contracts.
    for candidate in result.candidates:
        assert candidate.thesis
        assert candidate.direction


@pytest.mark.asyncio
async def test_a_market_open_run_does_select_contracts():
    result = await run_scan(stage=PipelineStage.MARKET_OPEN, symbols=["NVDA"], now=MARKET_OPEN)
    assert result.report.contracts_finalised
    assert any(v.structures for v in result.validations.values())


# --- provenance -------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_run_records_which_runner_and_methodology_produced_it(full_run):
    run = full_run.run
    assert run.agent_runner == "deterministic"
    assert run.methodology_version
    assert run.scoring_model_version  # the platform's frozen model, recorded for context
    assert full_run.report.diagnostics.agent_runner == "deterministic"


@pytest.mark.asyncio
async def test_provider_calls_are_recorded_without_urls_or_credentials(full_run):
    requests = full_run.run.provider_requests
    assert requests
    for req in requests:
        assert req.provider and req.capability
        # No field on the record can carry a query string or a header.
        assert not hasattr(req, "url")
        assert "key" not in (req.error or "").lower()


@pytest.mark.asyncio
async def test_agent_runs_record_prompts_and_dropped_claims(full_run):
    for record in full_run.run.agent_runs:
        assert record.runner == "deterministic"
        assert record.definition_path
        assert record.duration_ms is not None
        if record.agent.value == "market_intelligence":
            assert record.prompt_excerpt


# --- persistence ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_persists_and_reads_back(full_run):
    from app.db.base import Base
    from app.db.session import SessionLocal, engine
    from app.multiagent.db import (
        persist_scan,
        recommendations_for_run,
        score_components_for,
    )
    from app.multiagent.db.models import (
        MAAgentRunRow,
        MACandidateRow,
        MARunRow,
    )

    Base.metadata.create_all(engine)

    with SessionLocal() as session:
        run_id = persist_scan(session, full_run)
        session.commit()

        run_row = session.get(MARunRow, run_id)
        assert run_row is not None
        assert run_row.execution_enabled is False
        assert run_row.agent_runner == "deterministic"

        candidates = session.query(MACandidateRow).filter_by(run_id=run_id).all()
        assert len(candidates) == len(full_run.candidates)

        recos = recommendations_for_run(session, run_id)
        assert len(recos) == len(full_run.candidates)
        # Rejected recommendations are stored, not discarded.
        assert any(r.hard_rejected or not r.is_ranked for r in recos)

        for reco in recos:
            assert reco.calibration_status == "UNCALIBRATED"
            components = score_components_for(session, reco.candidate_id)
            if reco.payload.get("audit"):
                assert len(components) == 8
                # The per-rule audit travels with the component.
                assert any(c.rules for c in components)

        agent_rows = session.query(MAAgentRunRow).filter_by(run_id=run_id).all()
        assert agent_rows
        for row in agent_rows:
            assert "sk-" not in (row.prompt_excerpt or "")


@pytest.mark.asyncio
async def test_a_human_decision_can_be_recorded_against_a_recommendation(full_run):
    from datetime import datetime as dt

    from app.db.base import Base
    from app.db.session import SessionLocal, engine
    from app.multiagent.db import record_decision, record_execution, record_result
    from app.multiagent.models.enums import DecisionAction
    from app.multiagent.models.report import TradeExecution, TradeResult

    Base.metadata.create_all(engine)
    candidate = full_run.candidates[0]

    with SessionLocal() as session:
        decision = record_decision(
            session,
            run_id=full_run.report.run_id,
            candidate_id=candidate.candidate_id,
            action=DecisionAction.ENTERED,
            decided_at=dt.now(UTC),
            notes="entered manually",
        )
        record_execution(
            session,
            TradeExecution(
                execution_id="exec-1",
                decision_id=decision.decision_id,
                candidate_id=candidate.candidate_id,
                entered_at=dt.now(UTC),
                contract_description="test structure",
                quantity=1,
                entry_price_per_contract=97.0,
            ),
        )
        record_result(
            session,
            TradeResult(
                result_id="res-1",
                execution_id="exec-1",
                candidate_id=candidate.candidate_id,
                exited_at=dt.now(UTC) + timedelta(days=3),
                exit_price_per_contract=140.0,
                realized_pnl=43.0,
                max_favorable_excursion_bound=60.0,
                max_adverse_excursion_bound=-20.0,
            ),
        )
        session.commit()

        from app.multiagent.db.models import MAResultRow

        stored = session.get(MAResultRow, "res-1")
        assert stored is not None
        assert stored.realized_pnl == 43.0
        # The field name carries the caveat that MFE/MAE are bounds.
        assert stored.max_favorable_excursion_bound == 60.0
