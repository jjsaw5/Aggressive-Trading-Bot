"""Tier evaluators: lightweight Tier 1, medium Tier 2, deep Tier 3, positions T4."""

from __future__ import annotations

from app.engine.candidate_builder import ScanEngine
from app.engine.universe import UniverseConfig
from app.providers.mock import MockProvider
from app.quant.pricing import plan_entry_net_per_share
from app.risk.policy import RiskPolicy
from app.services.paper_engine import open_paper_trade
from app.tiers.tier1_broad import Tier1BroadScanner, score_tier1
from app.tiers.tier2_watchlist import Tier2WatchlistScanner
from app.tiers.tier3_candidates import Tier3CandidateEvaluator
from app.tiers.tier4_positions import Tier4PositionMonitor, mark_net_per_share

_SYMS = ["SPY", "AAPL", "NVDA", "AMD", "QQQ"]


def _policy() -> RiskPolicy:
    return RiskPolicy(
        account_equity_usd=2000, max_account_risk_pct=0.15, max_trade_risk_pct=0.05,
        max_concurrent_positions=8, max_defined_risk_per_trade_usd=100,
    )


def test_score_tier1_is_bounded_and_weighted() -> None:
    assert score_tier1(0.0, 1.0, False) == 0.0
    assert score_tier1(10.0, 5.0, True) == 1.0  # saturates
    mid = score_tier1(2.5, 2.0, True)  # half gap, ~third relvol, catalyst
    assert 0.0 < mid < 1.0


async def test_tier1_broad_lightweight() -> None:
    mock = MockProvider()
    t1 = Tier1BroadScanner(
        market=mock, fundamentals=mock, calendar=mock,
        universe=UniverseConfig(symbols=_SYMS),
    )
    results = await t1.run()
    assert len(results) == len(_SYMS)
    # Sorted by score descending; every result has the lightweight fields.
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)
    assert all(hasattr(r, "gap_pct") and hasattr(r, "rel_volume") for r in results)


async def test_tier2_watchlist_scores_and_direction() -> None:
    mock = MockProvider()
    t2 = Tier2WatchlistScanner(market=mock, flow=mock, chain=mock, iv_history=mock)
    results = await t2.run(_SYMS)
    assert len(results) == len(_SYMS)
    assert all(0.0 <= r.score <= 1.0 for r in results)
    assert results == sorted(results, key=lambda r: r.score, reverse=True)


async def test_tier3_reuses_scan_engine() -> None:
    # Uses build_scan_engine -> registry (mock under test config).
    t3 = Tier3CandidateEvaluator()
    candidates = await t3.run(["SPY", "AAPL", "NVDA"])
    assert len(candidates) == 3
    assert all(c.scan_id for c in candidates)


def _actionable_trade():
    mock = MockProvider()
    return mock


async def test_tier4_marks_open_position() -> None:
    mock = MockProvider()
    engine = ScanEngine(
        market=mock, fundamentals=mock, chain=mock, flow=mock, calendar=mock,
        policy=_policy(), universe=UniverseConfig(symbols=_SYMS),
    )
    cands = await engine.run()
    actionable = next((c for c in cands if c.is_actionable), None)
    if actionable is None:
        return  # nothing actionable this run
    entry = plan_entry_net_per_share(actionable.trade_plan)
    trade = open_paper_trade(actionable.trade_plan, actionable.scan_id, entry_mid=entry)

    monitor = Tier4PositionMonitor(chain=mock)
    risks = await monitor.run([trade])
    assert len(risks) == 1
    risk = risks[0]
    assert risk.symbol == actionable.symbol
    assert risk.trade_id == trade.id
    assert risk.action in {"hold", "take_profit", "stop", "time_stop"}
    assert risk.current_net is not None


async def test_mark_net_per_share_matches_legs() -> None:
    mock = MockProvider()
    engine = ScanEngine(
        market=mock, fundamentals=mock, chain=mock, flow=mock, calendar=mock,
        policy=_policy(), universe=UniverseConfig(symbols=_SYMS),
    )
    cands = await engine.run()
    actionable = next((c for c in cands if c.is_actionable), None)
    if actionable is None:
        return
    chain = await mock.get_option_chain(actionable.symbol)
    net = mark_net_per_share(actionable.trade_plan, chain)
    assert net is not None  # every leg found a matching contract mark
