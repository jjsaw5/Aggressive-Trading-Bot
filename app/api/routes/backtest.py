"""Backtest endpoints — simulated (Monte-Carlo) or historical (real paths).

Answers platform Question 14 (historical performance of the setup).
- simulated: zero-drift GBM paths; validates structure/exits, no assumed edge.
- historical: replays REAL underlying price history through the same engine
  (real market data when FMP/Robinhood is configured; legs repriced by
  Black-Scholes at trailing realized vol).
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.backtest.runner import run_backtest, run_historical_backtest
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


@router.post("/historical")
async def create_historical_backtest(
    lookback_days: int = Query(default=365, ge=90, le=2000),
) -> dict:
    report = await run_historical_backtest(
        universe=UniverseConfig(), lookback_days=lookback_days
    )
    result = report.as_dict()
    result["disclaimer"] = (
        "Real underlying price paths; option legs repriced with Black-Scholes at "
        "trailing realized vol (not recorded option quotes). Data source depends on "
        "PROVIDER_MARKET_DATA."
    )
    return result
