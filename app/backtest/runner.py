"""Backtest runner: scan -> simulate forward paths -> backtest -> report.

For each actionable candidate it Monte-Carlo simulates forward underlying paths
and backtests the candidate's sized trade plan over each, then aggregates by
setup type.

HONESTY NOTE: paths are simulated with **zero drift** (a martingale/GBM at the
risk-free rate), so any edge shown comes from the structure and exit rules, not
from an assumed favorable move — this avoids overstating performance. Swap in
real historical option marks for production backtests (see module docstring).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.backtest.engine import BacktestResult, backtest_trade
from app.backtest.historical import HistoricalConfig, replay_symbol
from app.backtest.performance import (
    PerformanceStats,
    by_direction,
    by_strategy,
    overall,
)
from app.domain.candidates import TradeCandidate
from app.engine.universe import UniverseConfig
from app.logging_config import get_logger
from app.providers import registry
from app.risk.policy import RiskPolicy
from app.services.scan_service import run_scan

log = get_logger(__name__)

_TRADING_DAYS = 252
_DEFAULT_VOL = 0.40
_RISK_FREE = 0.04


@dataclass
class BacktestReport:
    num_candidates: int
    num_paths: int
    num_trades: int
    overall: PerformanceStats
    by_strategy: list[PerformanceStats]
    by_direction: list[PerformanceStats]
    mode: str = "simulated"

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "num_candidates": self.num_candidates,
            "num_paths": self.num_paths,
            "num_trades": self.num_trades,
            "overall": self.overall.as_dict(),
            "by_strategy": [s.as_dict() for s in self.by_strategy],
            "by_direction": [s.as_dict() for s in self.by_direction],
        }


def _candidate_vol(candidate: TradeCandidate) -> float:
    vol_sig = next((s for s in candidate.signals if s.name == "volatility"), None)
    if vol_sig is not None:
        iv30 = vol_sig.details.get("iv30")
        if isinstance(iv30, (int, float)) and iv30 > 0:
            return float(iv30)
    return _DEFAULT_VOL


def _entry_dte(candidate: TradeCandidate) -> int:
    plan = candidate.trade_plan
    assert plan is not None
    exp = min(leg.expiration for leg in plan.legs)
    return max(1, (exp - candidate.generated_at.date()).days)


def _entry_spot(candidate: TradeCandidate) -> float:
    """True underlying spot at scan time (from the price-action signal)."""
    pa = next((s for s in candidate.signals if s.name == "price_action"), None)
    if pa is not None:
        px = pa.details.get("price")
        if isinstance(px, (int, float)) and px > 0:
            return float(px)
    # Fallback: the long leg strike is near-the-money by construction.
    assert candidate.trade_plan is not None
    return candidate.trade_plan.legs[0].strike


def _simulate_paths(
    spot: float, vol: float, days: int, num_paths: int, rng: np.random.Generator
) -> np.ndarray:
    """Zero-excess-drift GBM. Returns array shape (num_paths, days+1)."""
    dt = 1.0 / _TRADING_DAYS
    drift = (_RISK_FREE - 0.5 * vol * vol) * dt
    diffusion = vol * np.sqrt(dt)
    shocks = rng.standard_normal((num_paths, days))
    log_steps = drift + diffusion * shocks
    log_paths = np.cumsum(log_steps, axis=1)
    paths = spot * np.exp(log_paths)
    return np.hstack([np.full((num_paths, 1), spot), paths])


async def run_backtest(
    universe: UniverseConfig | None = None,
    *,
    num_paths: int = 200,
    seed: int = 12345,
) -> BacktestReport:
    candidates = await run_scan(universe=universe)
    actionable = [c for c in candidates if c.is_actionable]
    # The engine now prices structures by SIGNED net, so credit spreads and iron
    # condors are scored with credit-aware exits (manage at a % of credit).
    rng = np.random.default_rng(seed)

    results: list[BacktestResult] = []
    for c in actionable:
        plan = c.trade_plan
        assert plan is not None
        vol = _candidate_vol(c)
        dte = _entry_dte(c)
        spot = _entry_spot(c)
        paths = _simulate_paths(spot, vol, dte, num_paths, rng)
        for i in range(num_paths):
            # reprice_entry keeps entry on the same BS curve as the path, so a
            # zero-drift sim has ~zero edge minus costs (no spurious alpha).
            res = backtest_trade(
                plan, dte, [float(x) for x in paths[i]], vol,
                scan_id=c.scan_id, reprice_entry=True,
            )
            results.append(res)

    report = BacktestReport(
        num_candidates=len(actionable),
        num_paths=num_paths,
        num_trades=len(results),
        overall=overall(results),
        by_strategy=by_strategy(results),
        by_direction=by_direction(results),
        mode="simulated",
    )
    log.info(
        "backtest_complete",
        mode="simulated",
        candidates=len(actionable),
        trades=len(results),
        win_rate=round(report.overall.win_rate, 3),
        expectancy=round(report.overall.expectancy_usd, 2),
    )
    return report


async def run_historical_backtest(
    universe: UniverseConfig | None = None,
    *,
    lookback_days: int = 365,
    config: HistoricalConfig | None = None,
    policy: RiskPolicy | None = None,
) -> BacktestReport:
    """Backtest a trend-following defined-risk-vertical strategy over REAL
    historical underlying paths, sourced through the market-data provider.

    With `PROVIDER_MARKET_DATA=fmp` (or robinhood) this uses real market history;
    with the mock it uses the deterministic synthetic history. Same code path.
    """
    universe = universe or UniverseConfig()
    config = config or HistoricalConfig()
    policy = policy or RiskPolicy.from_settings()
    market = registry.market_data_provider()

    results: list[BacktestResult] = []
    symbols = universe.normalized_symbols()
    for symbol in symbols:
        try:
            history = await market.get_price_history(symbol, lookback_days=lookback_days)
        except Exception as exc:
            log.warning("history_fetch_failed", symbol=symbol, error=str(exc))
            continue
        results.extend(replay_symbol(symbol, history, policy, config))

    report = BacktestReport(
        num_candidates=len(symbols),
        num_paths=0,
        num_trades=len(results),
        overall=overall(results),
        by_strategy=by_strategy(results),
        by_direction=by_direction(results),
        mode="historical",
    )
    log.info(
        "historical_backtest_complete",
        mode="historical",
        source=market.name,
        symbols=len(symbols),
        trades=len(results),
        win_rate=round(report.overall.win_rate, 3),
        expectancy=round(report.overall.expectancy_usd, 2),
    )
    return report
