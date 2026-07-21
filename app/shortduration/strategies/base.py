"""Short-duration strategy modules — shared contracts.

Each strategy is an INDEPENDENT, configurable module that answers one question:
*does a valid setup of my archetype exist right now?* It is setup-first — the
option is not considered here; a detection describes the market setup, its
direction, the entry trigger, and the invalidation. Contract selection (Phase 4)
decides whether a short-dated option is the right expression.

A detection carries a PROVISIONAL `setup_score` in [0,1] built from its own
confirmations. The formal, per-DTE scoring model replaces this in Phase 3 — the
field name is stable so that swap is localized.

Missing data is never treated as confirmation: a detector returns None when the
inputs it needs are absent, rather than assuming a neutral pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.domain.enums import Direction, DTECategory, ShortDurationStrategy
from app.domain.market import CatalystEvent, PriceHistory, Quote
from app.domain.options import FlowAlert
from app.domain.shortduration import (
    IntradayBar,
    IntradayLevels,
    NewsItem,
    ShortDurationRegimeState,
)


@dataclass
class SetupContext:
    """Everything a strategy may inspect for one symbol at one instant. Fields
    are best-effort — a provider miss leaves the field empty/None, and detectors
    guard for it."""

    symbol: str
    now: datetime
    regime: ShortDurationRegimeState
    change_pct: float | None = None
    levels: IntradayLevels | None = None
    bars_1m: list[IntradayBar] = field(default_factory=list)
    daily: PriceHistory | None = None
    quote: Quote | None = None
    flow: list[FlowAlert] = field(default_factory=list)
    catalysts: list[CatalystEvent] = field(default_factory=list)
    news: list[NewsItem] = field(default_factory=list)


@dataclass
class StrategyDetection:
    """A confirmed setup. Non-executing; describes what and why."""

    strategy: ShortDurationStrategy
    dte_category: DTECategory
    direction: Direction
    setup_score: float  # provisional [0,1] (formal model: Phase 3)
    entry_trigger: str
    invalidation: str
    reasons: list[str] = field(default_factory=list)
    targets: list[float] = field(default_factory=list)
    # Structured, strategy-specific diagnostics for observability (e.g. the
    # breakout buffer, extension, confirmation mode). Never used for gating.
    metadata: dict[str, float | str] = field(default_factory=dict)


# --- shared helpers ----------------------------------------------------------
def net_flow_sentiment(flow: list[FlowAlert]) -> float | None:
    """Average normalized sentiment across recent flow, or None if unavailable."""
    vals = [f.sentiment for f in flow if f.sentiment is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def flow_confirms(flow: list[FlowAlert], direction: Direction) -> bool | None:
    """Does aggregate flow agree with the setup direction? None if no flow."""
    s = net_flow_sentiment(flow)
    if s is None:
        return None
    if direction == Direction.BULLISH:
        return s > 0.15
    if direction == Direction.BEARISH:
        return s < -0.15
    return None


def regime_supports(regime: ShortDurationRegimeState, direction: Direction) -> bool:
    """The intraday regime must not directly contradict the setup direction."""
    from app.domain.enums import ShortDurationRegime as R

    if direction == Direction.BULLISH and regime.regime == R.BEAR_TREND:
        return False
    if direction == Direction.BEARISH and regime.regime == R.BULL_TREND:
        return False
    return True


def clamp01(x: float) -> float:
    return round(max(0.0, min(1.0, x)), 4)
