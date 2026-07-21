"""Phase 7 — v2 validation sweep (end-to-end invariants + paper simulation)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.enums import CandidateState, DTECategory

_NOW = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)  # Fri 11:00 ET, market open


async def _scan_both() -> list:
    from app.shortduration.detection import run_detection

    a = await run_detection(DTECategory.ZERO_DTE, now=_NOW)
    b = await run_detection(DTECategory.SHORT_DTE, now=_NOW)
    return a + b


async def test_every_candidate_carries_the_full_v2_payload() -> None:
    cands = await _scan_both()
    assert cands
    for c in cands:
        # Scoring: a scorecard whose factor weights always sum to 100, versioned.
        assert c.scorecard is not None
        assert round(sum(f.weight for f in c.scorecard.factors), 2) == 100
        assert c.scoring_model_version and c.risk_policy_version
        assert c.scorecard.model_version == c.scoring_model_version
        # Risk & trade management: a structure-aware exit plan on every candidate.
        assert c.exit_plan is not None
        assert c.exit_plan.primary_invalidation and c.exit_plan.eod_action
        assert c.exit_plan.expiration_action
        # Data freshness recorded at scoring.
        assert c.freshness is not None and "ok" in c.freshness
        # Confidence is data-quality tempered — never above the raw normalized score.
        assert c.confidence <= c.score + 1e-9


async def test_0dte_signal_metadata_and_exit_clock() -> None:
    from app.shortduration.detection import run_detection

    cands = await run_detection(DTECategory.ZERO_DTE, now=_NOW)
    assert cands
    # At least one 0DTE candidate exposes strategy diagnostics, and every 0DTE exit
    # plan is clock-managed (flatten before the close — never held to settlement).
    assert any(c.signal_metadata for c in cands)
    for c in cands:
        assert c.exit_plan.eod_action.startswith("close_all")
        assert "Flatten" in c.exit_plan.time_stop


async def test_paper_simulation_book_coherence() -> None:
    from app.db.models import ShortDurationTradeRow
    from app.db.session import SessionLocal
    from app.shortduration.paper import (
        monitor_short_duration_positions,
        open_short_duration_paper,
        short_duration_performance,
    )

    cands = await _scan_both()
    tradeable = [c for c in cands if c.trade_plan is not None and c.state != CandidateState.REJECTED]
    if not tradeable:
        pytest.skip("no tradeable candidates in the mock universe")

    opened = []
    try:
        for c in tradeable[:8]:
            t = await open_short_duration_paper(c, now=_NOW)
            opened.append(t.id)
        # Monitoring marks/updates without raising.
        await monitor_short_duration_positions(now=_NOW)

        perf = short_duration_performance()
        ol = perf["opportunity_loss"]
        # Book B (account-executable) can never have more decided trades than Book A.
        assert perf["book_b"]["overall"].get("trades", 0) <= perf["overall"].get("trades", 0)
        # Opportunity-loss identity holds exactly.
        assert round(ol["book_a_total_pnl"] - ol["book_b_total_pnl"], 2) == ol["left_on_table_pnl"]
        assert ol["executable_decided"] + ol["not_executable_decided"] == ol["signals_decided"]
    finally:
        with SessionLocal() as s:
            for tid in opened:
                row = s.get(ShortDurationTradeRow, tid)
                if row is not None:
                    s.delete(row)
            s.commit()


async def test_scan_is_deterministic_on_the_mock_universe() -> None:
    from app.shortduration.detection import run_detection

    a = await run_detection(DTECategory.ZERO_DTE, now=_NOW)
    b = await run_detection(DTECategory.ZERO_DTE, now=_NOW)
    # Same inputs -> same symbols detected and same ranking (no hidden randomness).
    assert [c.symbol for c in a] == [c.symbol for c in b]
    assert [round(c.score, 6) for c in a] == [round(c.score, 6) for c in b]
