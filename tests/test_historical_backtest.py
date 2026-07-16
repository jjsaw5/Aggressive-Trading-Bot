"""Historical replay: realized vol, no-look-ahead direction, and real-path P&L."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.backtest.historical import (
    HistoricalConfig,
    annualized_realized_vol,
    as_of_direction,
    build_vertical_as_of,
    replay_symbol,
)
from app.domain.enums import Direction
from app.domain.market import Candle, PriceHistory
from app.risk.policy import RiskPolicy


def _history(closes: list[float]) -> PriceHistory:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [
        Candle(ts=base + timedelta(days=i), open=c, high=c, low=c, close=c, volume=1_000_000)
        for i, c in enumerate(closes)
    ]
    return PriceHistory(symbol="AAA", candles=candles, source="test")


@pytest.fixture
def roomy_policy() -> RiskPolicy:
    # Wider caps so mega-cap-priced spreads can be sized in tests.
    return RiskPolicy(
        account_equity_usd=2_000.0,
        max_account_risk_pct=0.30,
        max_trade_risk_pct=0.15,
        max_concurrent_positions=8,
        max_defined_risk_per_trade_usd=300.0,
        max_contracts_per_trade=20,
    )


def test_realized_vol_positive_and_scaled() -> None:
    closes = [100.0 * (1.01 if i % 2 else 0.99) for i in range(40)]
    vol = annualized_realized_vol(closes, end=30, window=20)
    assert vol is not None and vol > 0


def test_realized_vol_needs_history() -> None:
    assert annualized_realized_vol([100.0, 101.0], end=1, window=20) is None


def test_direction_detects_trend_without_lookahead() -> None:
    up = [100.0 + i for i in range(60)]
    down = [200.0 - i for i in range(60)]
    assert as_of_direction(up, 55, 20, 50) == Direction.BULLISH
    assert as_of_direction(down, 55, 20, 50) == Direction.BEARISH
    # Not enough history -> neutral.
    assert as_of_direction(up, 10, 20, 50) == Direction.NEUTRAL


def test_direction_is_causal() -> None:
    # A spike AFTER index i must not affect the direction at i.
    closes = [100.0 + i for i in range(60)]
    d_before = as_of_direction(closes, 55, 20, 50)
    closes[58] = 10_000.0  # future spike
    d_after = as_of_direction(closes, 55, 20, 50)
    assert d_before == d_after


def test_build_vertical_respects_cap(roomy_policy: RiskPolicy) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    plan = build_vertical_as_of(
        "AAA", spot=100.0, direction=Direction.BULLISH, vol=0.3, dte=21,
        as_of=now, policy=roomy_policy, width_pcts=(0.02, 0.04),
    )
    assert plan is not None
    assert plan.risk.max_loss_usd <= roomy_policy.max_trade_risk_usd + 1e-6
    assert len(plan.legs) == 2


def test_replay_uptrend_produces_bullish_trades(roomy_policy: RiskPolicy) -> None:
    # Steady uptrend with mild noise -> bullish setups that should mostly profit.
    closes = [100.0 * (1.004) ** i + (0.5 if i % 2 else -0.5) for i in range(140)]
    hist = _history(closes)
    results = replay_symbol("AAA", hist, roomy_policy, HistoricalConfig())
    assert results, "expected at least one historical trade"
    assert all(r.trade.trade_plan.direction == Direction.BULLISH for r in results)
    wins = sum(r.is_win for r in results)
    assert wins / len(results) > 0.5  # uptrend should favor bull call spreads
