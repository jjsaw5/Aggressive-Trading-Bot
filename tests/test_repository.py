"""Persistence repository round-trips (SQLite-backed)."""

from __future__ import annotations

from app.db import repository
from app.engine.candidate_builder import ScanEngine
from app.engine.universe import UniverseConfig
from app.modes.proposals import create_proposal
from app.providers.mock import MockProvider
from app.quant.pricing import plan_entry_net_per_share
from app.risk.policy import RiskPolicy
from app.services.paper_engine import open_paper_trade


async def _make_candidates() -> list:
    mock = MockProvider()
    engine = ScanEngine(
        market=mock, fundamentals=mock, chain=mock, flow=mock, calendar=mock,
        policy=RiskPolicy(
            account_equity_usd=2000, max_account_risk_pct=0.15, max_trade_risk_pct=0.05,
            max_concurrent_positions=4, max_defined_risk_per_trade_usd=100,
        ),
        universe=UniverseConfig(symbols=["SPY", "AAPL", "NVDA"]),
    )
    return await engine.run()


async def test_scan_and_candidate_roundtrip() -> None:
    cands = await _make_candidates()
    scan_id = cands[0].scan_id
    repository.save_scan(scan_id, ["SPY", "AAPL", "NVDA"], cands)

    scans = repository.list_scans(limit=5)
    assert any(s.scan_id == scan_id for s in scans)

    loaded = repository.get_scan_candidates(scan_id)
    assert len(loaded) == len(cands)
    # Sorted by score desc, and payloads deserialize back to full candidates.
    scores = [c.composite_score for c in loaded]
    assert scores == sorted(scores, reverse=True)
    assert loaded[0].thesis.why_now  # rich payload preserved


async def test_proposal_roundtrip_and_update() -> None:
    cands = await _make_candidates()
    actionable = next((c for c in cands if c.is_actionable), None)
    scan_id = cands[0].scan_id
    repository.save_scan(scan_id, ["SPY", "AAPL", "NVDA"], cands)
    if actionable is None:
        return  # nothing actionable this run; roundtrip covered elsewhere

    prop = create_proposal(actionable)
    repository.save_proposal(prop)

    loaded = repository.get_proposal(prop.id)
    assert loaded is not None and loaded.symbol == actionable.symbol

    # Update status and confirm merge persists.
    from app.domain.enums import ProposalStatus

    loaded.status = ProposalStatus.APPROVED
    loaded.approved_by = "tester"
    repository.save_proposal(loaded)
    again = repository.get_proposal(prop.id)
    assert again is not None and again.status == ProposalStatus.APPROVED
    assert again.approved_by == "tester"


async def test_paper_trade_roundtrip() -> None:
    cands = await _make_candidates()
    actionable = next((c for c in cands if c.is_actionable), None)
    if actionable is None or actionable.trade_plan is None:
        return
    entry = plan_entry_net_per_share(actionable.trade_plan)
    trade = open_paper_trade(actionable.trade_plan, actionable.scan_id, entry_mid=entry)
    repository.save_paper_trade(trade)

    loaded = repository.get_paper_trade(trade.id)
    assert loaded is not None and loaded.symbol == actionable.symbol
    trades = repository.list_paper_trades(limit=10)
    assert any(t.id == trade.id for t in trades)
