"""Scoring, direction resolution, and confirmation logic."""

from __future__ import annotations

from app.domain.enums import Direction
from app.domain.signals import SignalBundle, SignalScore
from app.engine.scoring import (
    ScoreWeights,
    composite_score,
    confirmation_multiplier,
    resolve_direction,
)


def _sig(name: str, score: float, direction: Direction | None = None) -> SignalScore:
    return SignalScore(name=name, score=score, direction=direction)


def test_confirmation_boosts_when_flow_and_price_agree() -> None:
    flow = _sig("options_flow", 0.8, Direction.BULLISH)
    price = _sig("price_action", 0.7, Direction.BULLISH)
    assert confirmation_multiplier(flow, price) == 1.15


def test_conflict_penalizes() -> None:
    flow = _sig("options_flow", 0.8, Direction.BULLISH)
    price = _sig("price_action", 0.7, Direction.BEARISH)
    assert confirmation_multiplier(flow, price) == 0.7


def test_direction_prefers_flow() -> None:
    flow = _sig("options_flow", 0.8, Direction.BEARISH)
    price = _sig("price_action", 0.7, Direction.NEUTRAL)
    assert resolve_direction(flow, price) == Direction.BEARISH


def test_confirmed_setup_scores_higher_than_conflicted() -> None:
    weights = ScoreWeights()
    confirmed = SignalBundle(
        symbol="AAA",
        scores=[
            _sig("options_flow", 0.8, Direction.BULLISH),
            _sig("price_action", 0.8, Direction.BULLISH),
            _sig("volatility", 0.6),
            _sig("catalyst", 0.5),
        ],
    )
    conflicted = SignalBundle(
        symbol="BBB",
        scores=[
            _sig("options_flow", 0.8, Direction.BULLISH),
            _sig("price_action", 0.8, Direction.BEARISH),
            _sig("volatility", 0.6),
            _sig("catalyst", 0.5),
        ],
    )
    assert composite_score(confirmed, weights) > composite_score(conflicted, weights)


def test_score_bounded_0_1() -> None:
    bundle = SignalBundle(
        symbol="AAA",
        scores=[
            _sig("options_flow", 1.0, Direction.BULLISH),
            _sig("price_action", 1.0, Direction.BULLISH),
            _sig("volatility", 1.0),
            _sig("catalyst", 1.0),
        ],
    )
    assert 0.0 <= composite_score(bundle) <= 1.0
