"""Backtest endpoint — run a simulated backtest of actionable candidates.

Answers platform Question 14 (historical performance of the setup). Paths are
simulated (zero-drift GBM), not real option history — see the runner docstring.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.backtest.runner import run_backtest
from app.engine.universe import UniverseConfig

router = APIRouter(prefix="/backtest", tags=["backtest"])


@router.post("")
async def create_backtest(
    num_paths: int = Query(default=200, ge=10, le=2000),
    seed: int = Query(default=12345),
) -> dict:
    report = await run_backtest(universe=UniverseConfig(), num_paths=num_paths, seed=seed)
    result = report.as_dict()
    result["disclaimer"] = (
        "Simulated zero-drift paths, not historical option marks. Indicative of "
        "structural/exit behavior only."
    )
    return result
