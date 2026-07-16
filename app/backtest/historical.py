"""Historical backtest over REAL underlying price paths.

Instead of simulated GBM, this replays actual historical underlying candles
(sourced through `MarketDataProvider.get_price_history` — real market data when
FMP/Robinhood is configured). For each historical entry date it reconstructs
the setup *as of that date* (trailing trend for direction, trailing realized
volatility for pricing), builds a sized defined-risk vertical, and backtests it
over the actual subsequent path.

This makes the dominant P&L driver — the real underlying path — genuine market
history, not a simulation. The remaining approximation is that option legs are
repriced with Black-Scholes at trailing realized vol rather than from recorded
option quotes; when a `HistoricalOptionsProvider` is available, swap in real
per-contract marks (the engine already accepts explicit marks via the path).

No look-ahead: direction and vol at entry use only data up to the entry index;
the forward path uses only subsequent candles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.backtest.engine import BacktestResult, backtest_trade
from app.domain.enums import Direction, OptionType
from app.domain.market import PriceHistory
from app.domain.options import Greeks, OptionContract
from app.engine.contract_selection import SpreadChoice
from app.quant.pricing import black_scholes_delta, black_scholes_price
from app.risk.policy import RiskPolicy
from app.risk.trade_plan import build_vertical_spread_plan

_TRADING_DAYS = 252


@dataclass(frozen=True)
class HistoricalConfig:
    vol_window: int = 20          # trailing days for realized vol
    trend_fast: int = 20          # SMA fast
    trend_slow: int = 50          # SMA slow
    hold_days: int = 21           # trading-day horizon (~1 month option to expiry)
    entry_cadence: int = 5        # enter every N trading days
    width_pcts: tuple[float, ...] = (0.01, 0.02, 0.03)  # candidate spread widths
    min_vol: float = 0.05
    max_vol: float = 2.0


def annualized_realized_vol(closes: list[float], end: int, window: int) -> float | None:
    """Annualized stdev of daily log returns over closes[end-window:end]."""
    if end < window + 1:
        return None
    seg = closes[end - window : end + 1]
    rets = [math.log(seg[k] / seg[k - 1]) for k in range(1, len(seg)) if seg[k - 1] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(_TRADING_DAYS)


def as_of_direction(closes: list[float], i: int, fast: int, slow: int) -> Direction:
    """Trend direction using only data up to index i (no look-ahead)."""
    if i < slow:
        return Direction.NEUTRAL
    sma_fast = sum(closes[i - fast + 1 : i + 1]) / fast
    sma_slow = sum(closes[i - slow + 1 : i + 1]) / slow
    if sma_fast > sma_slow:
        return Direction.BULLISH
    if sma_fast < sma_slow:
        return Direction.BEARISH
    return Direction.NEUTRAL


def _leg(symbol: str, strike: float, otype: OptionType, price: float, spot: float,
         t: float, vol: float, exp, as_of_dt: datetime) -> OptionContract:
    half = max(0.01, price * 0.02) / 2
    return OptionContract(
        symbol=symbol,
        option_symbol=f"{symbol}{exp:%y%m%d}{'C' if otype == OptionType.CALL else 'P'}"
        f"{int(strike * 1000):08d}",
        expiration=exp,
        strike=strike,
        option_type=otype,
        bid=round(max(0.01, price - half), 2),
        ask=round(price + half, 2),
        mark=round(price, 2),
        last=round(price, 2),
        volume=1000,
        open_interest=3000,
        implied_volatility=round(vol, 4),
        greeks=Greeks(delta=round(black_scholes_delta(spot, strike, t, vol, otype), 3)),
        as_of=as_of_dt,
    )


def build_vertical_as_of(
    symbol: str,
    spot: float,
    direction: Direction,
    vol: float,
    dte: int,
    as_of: datetime,
    policy: RiskPolicy,
    width_pcts: tuple[float, ...],
):
    """Construct a sized defined-risk vertical priced by BS as of the entry.

    Tries increasing widths and returns the first that sizes to >=1 contract
    within the risk policy (matching the live engine's spread structure).
    """
    if direction not in (Direction.BULLISH, Direction.BEARISH):
        return None
    otype = OptionType.CALL if direction == Direction.BULLISH else OptionType.PUT
    t = dte / 365.0
    exp = as_of.date() + timedelta(days=dte)
    long_strike = round(spot)

    best = None
    for wp in width_pcts:
        width = max(0.5, round(spot * wp * 2) / 2)
        if direction == Direction.BULLISH:
            short_strike = long_strike + width
        else:
            short_strike = long_strike - width
        if short_strike <= 0:
            continue

        long_px = black_scholes_price(spot, long_strike, t, vol, otype)
        short_px = black_scholes_price(spot, short_strike, t, vol, otype)
        debit = long_px - short_px
        if debit <= 0:
            continue

        long_leg = _leg(symbol, long_strike, otype, long_px, spot, t, vol, exp, as_of)
        short_leg = _leg(symbol, short_strike, otype, short_px, spot, t, vol, exp, as_of)
        spread = SpreadChoice(
            long_leg=long_leg,
            short_leg=short_leg,
            net_debit_per_share=round(debit, 4),
            width=round(width, 2),
            max_loss_per_contract=round(debit * 100, 2),
            max_profit_per_contract=round((width - debit) * 100, 2),
            fit_score=0.0,
        )
        plan = build_vertical_spread_plan(spread, direction, policy, as_of.date())
        if plan is not None:
            best = plan
            break
    return best


def replay_symbol(
    symbol: str,
    history: PriceHistory,
    policy: RiskPolicy,
    cfg: HistoricalConfig | None = None,
) -> list[BacktestResult]:
    cfg = cfg or HistoricalConfig()
    closes = history.closes
    n = len(closes)
    results: list[BacktestResult] = []

    start = max(cfg.trend_slow, cfg.vol_window)
    last_entry = n - cfg.hold_days - 1
    for i in range(start, last_entry + 1, cfg.entry_cadence):
        spot = closes[i]
        if spot <= 0:
            continue
        direction = as_of_direction(closes, i, cfg.trend_fast, cfg.trend_slow)
        if direction == Direction.NEUTRAL:
            continue
        vol = annualized_realized_vol(closes, i, cfg.vol_window)
        if vol is None or not (cfg.min_vol <= vol <= cfg.max_vol):
            continue

        as_of_dt = history.candles[i].ts if i < len(history.candles) else datetime.now(UTC)
        plan = build_vertical_as_of(
            symbol, spot, direction, vol, cfg.hold_days, as_of_dt, policy, cfg.width_pcts
        )
        if plan is None:
            continue

        forward = closes[i : i + cfg.hold_days + 1]
        res = backtest_trade(
            plan, cfg.hold_days, forward, vol, scan_id=f"hist:{symbol}", reprice_entry=True
        )
        results.append(res)
    return results
