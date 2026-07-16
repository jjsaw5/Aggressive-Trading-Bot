"""Scan service: assembles a `ScanEngine` from configured providers and runs it.

This is the single composition point where the abstract provider registry meets
the engine. Callers (API routes, scheduler, CLI) depend on this, not on
concrete providers.
"""

from __future__ import annotations

from app.domain.candidates import TradeCandidate
from app.engine.candidate_builder import ScanEngine
from app.engine.scoring import ScoreWeights
from app.engine.universe import UniverseConfig
from app.providers import registry
from app.risk.policy import RiskPolicy
from app.risk.portfolio import PortfolioState


def build_scan_engine(
    universe: UniverseConfig | None = None,
    weights: ScoreWeights | None = None,
    policy: RiskPolicy | None = None,
) -> ScanEngine:
    return ScanEngine(
        market=registry.market_data_provider(),
        fundamentals=registry.fundamentals_provider(),
        chain=registry.options_chain_provider(),
        flow=registry.options_flow_provider(),
        calendar=registry.calendar_provider(),
        iv_history=registry.iv_history_provider(),
        policy=policy or RiskPolicy.from_settings(),
        universe=universe or UniverseConfig(),
        weights=weights or ScoreWeights(),
    )


async def run_scan(
    universe: UniverseConfig | None = None,
    portfolio: PortfolioState | None = None,
) -> list[TradeCandidate]:
    engine = build_scan_engine(universe=universe)
    return await engine.run(portfolio=portfolio)
