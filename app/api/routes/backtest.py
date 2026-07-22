"""Backtest endpoints — simulated (Monte-Carlo) or historical (real paths).

Answers platform Question 14 (historical performance of the setup).
- simulated: zero-drift GBM paths; validates structure/exits, no assumed edge.
- historical: replays REAL underlying price history through the same engine
  (real market data when FMP/Robinhood is configured; legs repriced by
  Black-Scholes at trailing realized vol).
"""

from __future__ import annotations

from datetime import date

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


# Small on-demand populations per regime (the full sweep lives in scripts/).
_RM_REGIMES = {
    "2021-22": (
        [("SPY", list(range(360, 461, 20)), 20), ("AAPL", list(range(130, 171, 10)), 10)],
        (date(2021, 12, 1), date(2022, 6, 30)),
    ),
    "2023-24": (
        [("SPY", list(range(420, 561, 20)), 20), ("AAPL", list(range(160, 221, 10)), 10)],
        (date(2023, 6, 1), date(2024, 6, 30)),
    ),
}


@router.get("/real-mark")
async def real_mark_backtest(
    mode: str = Query(default="engine", pattern="^(engine|fixed)$"),
    regime: str = Query(default="2021-22", pattern="^(2021-22|2023-24)$"),
) -> dict:
    """VALIDATING backtest over real recorded option marks (UW per-contract
    history), net of round-trip costs, at both k=0.5 and k=1.0. Runs a small
    default population live for the chosen regime; gated on the UW historic
    entitlement — returns ``available=false`` with the reason when it's off,
    rather than erroring."""
    from app.backtest.real_mark_runner import build_and_run
    from app.backtest.real_mark_seed import monthly_expiries
    from app.providers import registry
    from app.providers.registry import ProviderConfigError

    try:
        provider = registry.historical_options_provider()
    except ProviderConfigError as exc:
        return {"available": False, "validating": False, "reason": str(exc)}

    universe, (start, end) = _RM_REGIMES[regime]
    expiries = monthly_expiries(start, end)[::2]
    report = await build_and_run(provider, universe, expiries, mode=mode)
    await provider.aclose()
    result = report.as_dict()
    result["available"] = True
    result["mode"] = mode
    result["regime"] = regime
    result["disclaimer"] = (
        "Real recorded NBBO marks, net of commissions + spread crossing. A small "
        "default SPY/AAPL population; the full multi-year sweep is scripts/real_mark_backtest.py."
    )
    return result
