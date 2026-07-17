"""Tier 3 — trade-candidate deep evaluation.

Full depth: option chain, greeks, contract selection, risk/reward, exit plan.
This is exactly the existing `ScanEngine` pipeline, reused verbatim over the
small promoted set (3-10 names). Runs at CANDIDATES priority. Actionable output
is the funnel's trade-proposal surface.
"""

from __future__ import annotations

from collections.abc import Callable

from app.domain.candidates import TradeCandidate
from app.engine.universe import UniverseConfig
from app.logging_config import get_logger
from app.providers.ratelimit import Priority, use_priority
from app.services.scan_service import build_scan_engine

log = get_logger(__name__)


class Tier3CandidateEvaluator:
    def __init__(self, engine_factory: Callable = build_scan_engine) -> None:
        # Injectable so tests can supply a mock-backed engine.
        self._factory = engine_factory

    async def run(self, symbols: list[str]) -> list[TradeCandidate]:
        if not symbols:
            return []
        engine = self._factory(universe=UniverseConfig(symbols=symbols))
        with use_priority(Priority.CANDIDATES):
            candidates = await engine.run()
        log.info(
            "tier3_complete",
            evaluated=len(symbols),
            actionable=sum(c.is_actionable for c in candidates),
        )
        return candidates
