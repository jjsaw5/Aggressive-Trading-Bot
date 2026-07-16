"""End-to-end scan pipeline over the deterministic mock provider."""

from __future__ import annotations

import pytest

from app.domain.enums import CandidateStatus
from app.engine.candidate_builder import ScanEngine
from app.engine.universe import UniverseConfig
from app.providers.mock import MockProvider
from app.risk.policy import RiskPolicy


@pytest.fixture
def engine(policy: RiskPolicy) -> ScanEngine:
    mock = MockProvider()
    return ScanEngine(
        market=mock,
        fundamentals=mock,
        chain=mock,
        flow=mock,
        calendar=mock,
        policy=policy,
        universe=UniverseConfig(),
    )


async def test_scan_produces_ranked_candidates(engine: ScanEngine) -> None:
    candidates = await engine.run()
    assert candidates, "scan should produce candidates"
    # Sorted descending by score.
    scores = [c.composite_score for c in candidates]
    assert scores == sorted(scores, reverse=True)


async def test_every_candidate_answers_the_thesis(engine: ScanEngine) -> None:
    candidates = await engine.run()
    for c in candidates:
        assert c.thesis.why_now
        assert c.thesis.direction is not None
        assert 0.0 <= c.composite_score <= 1.0


async def test_actionable_candidates_have_defined_risk(engine: ScanEngine) -> None:
    candidates = await engine.run()
    actionable = [c for c in candidates if c.is_actionable]
    for c in actionable:
        assert c.trade_plan is not None
        # Defined risk must respect the per-trade cap.
        assert c.trade_plan.risk.max_loss_usd <= engine.policy.max_trade_risk_usd + 1e-6
        assert c.trade_plan.contracts >= 1
        assert c.trade_plan.risk.invalidation_note


async def test_rejected_candidates_carry_reasons(engine: ScanEngine) -> None:
    candidates = await engine.run()
    for c in candidates:
        if c.status == CandidateStatus.REJECTED:
            assert c.reject_reasons, f"{c.symbol} rejected without reasons"
