"""Price-action analyzer.

Answers: *is price action confirming the flow?* Computes trend + momentum from
daily closes and emits a directional `SignalScore`. Confirmation is judged
elsewhere (scoring) by comparing this direction to the flow direction.

Indicators (numpy, no TA dependency): SMA(20) vs SMA(50) trend, distance of
price above/below SMA20, and RSI(14) momentum.
"""

from __future__ import annotations

import numpy as np

from app.domain.enums import Direction
from app.domain.market import PriceHistory
from app.domain.signals import SignalScore


def _rsi(closes: np.ndarray, period: int = 14) -> float | None:
    if closes.size < period + 1:
        return None
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains[-period:].mean()
    avg_loss = losses[-period:].mean()
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def analyze_price_action(history: PriceHistory) -> SignalScore:
    closes = np.asarray(history.closes, dtype=float)
    if closes.size < 50:
        return SignalScore(
            name="price_action",
            score=0.0,
            direction=Direction.NEUTRAL,
            confidence=0.2,
            rationale="Insufficient price history (<50 bars).",
        )

    price = float(closes[-1])
    sma20 = float(closes[-20:].mean())
    sma50 = float(closes[-50:].mean())
    rsi = _rsi(closes)

    trend_up = sma20 > sma50
    dist = (price - sma20) / sma20 if sma20 else 0.0

    # Strength: how cleanly trend + momentum agree.
    strength = 0.0
    strength += 0.5 if trend_up else 0.0
    strength += min(0.3, abs(dist) * 6)  # distance from mean
    if rsi is not None:
        # Reward momentum in the trend direction, penalize exhaustion extremes.
        if trend_up:
            strength += 0.2 if 50 <= rsi <= 70 else (0.05 if rsi > 70 else 0.0)
        else:
            strength = strength  # handled by direction below

    if trend_up and dist > 0:
        direction = Direction.BULLISH
    elif not trend_up and dist < 0:
        direction = Direction.BEARISH
    else:
        direction = Direction.NEUTRAL

    # For bearish setups mirror the strength calc symmetrically.
    if direction == Direction.BEARISH:
        strength = 0.5 + min(0.3, abs(dist) * 6)
        if rsi is not None:
            strength += 0.2 if 30 <= rsi <= 50 else (0.05 if rsi < 30 else 0.0)

    score = round(min(1.0, strength), 4)
    return SignalScore(
        name="price_action",
        score=score if direction != Direction.NEUTRAL else round(score * 0.4, 4),
        direction=direction,
        confidence=0.6,
        rationale=(
            f"px {price:.2f}, SMA20 {sma20:.2f}, SMA50 {sma50:.2f}, "
            f"RSI {rsi:.0f}." if rsi is not None else
            f"px {price:.2f}, SMA20 {sma20:.2f}, SMA50 {sma50:.2f}."
        ),
        details={
            "price": round(price, 2),
            "sma20": round(sma20, 2),
            "sma50": round(sma50, 2),
            "rsi14": round(rsi, 1) if rsi is not None else None,
            "dist_from_sma20": round(dist, 4),
        },
    )
