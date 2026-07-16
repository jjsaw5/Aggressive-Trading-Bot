"""Composite scoring and direction resolution.

Combines the individual signal scores into a single ranked `composite_score`
and resolves the overall thesis direction. Confirmation between flow and price
action is a first-class multiplier: flow that is *confirmed* by price action
scores materially higher than flow alone (answering "is price confirming?").
"""

from __future__ import annotations

from pydantic import BaseModel

from app.domain.enums import Direction
from app.domain.signals import SignalBundle, SignalScore


class ScoreWeights(BaseModel):
    options_flow: float = 0.35
    price_action: float = 0.25
    volatility: float = 0.20
    catalyst: float = 0.20

    def normalized(self) -> dict[str, float]:
        total = self.options_flow + self.price_action + self.volatility + self.catalyst
        total = total or 1.0
        return {
            "options_flow": self.options_flow / total,
            "price_action": self.price_action / total,
            "volatility": self.volatility / total,
            "catalyst": self.catalyst / total,
        }


_DIRECTIONAL = {Direction.BULLISH, Direction.BEARISH}


def resolve_direction(flow: SignalScore | None, price: SignalScore | None) -> Direction:
    """Flow leads direction; price action breaks ties / confirms."""
    fd = flow.direction if flow else None
    pd = price.direction if price else None
    if fd in _DIRECTIONAL:
        return fd
    if pd in _DIRECTIONAL:
        return pd
    return Direction.NEUTRAL


def confirmation_multiplier(flow: SignalScore | None, price: SignalScore | None) -> float:
    """1.15 when flow and price agree directionally, 0.7 when they conflict,
    1.0 when one side is neutral/absent."""
    fd = flow.direction if flow else None
    pd = price.direction if price else None
    if fd in _DIRECTIONAL and pd in _DIRECTIONAL:
        return 1.15 if fd == pd else 0.7
    return 1.0


def composite_score(bundle: SignalBundle, weights: ScoreWeights | None = None) -> float:
    w = (weights or ScoreWeights()).normalized()
    flow = bundle.by_name("options_flow")
    price = bundle.by_name("price_action")
    vol = bundle.by_name("volatility")
    cat = bundle.by_name("catalyst")

    base = (
        w["options_flow"] * (flow.score if flow else 0.0)
        + w["price_action"] * (price.score if price else 0.0)
        + w["volatility"] * (vol.score if vol else 0.0)
        + w["catalyst"] * (cat.score if cat else 0.0)
    )
    score = base * confirmation_multiplier(flow, price)
    return round(min(1.0, max(0.0, score)), 4)
