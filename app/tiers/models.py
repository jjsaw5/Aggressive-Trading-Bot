"""Tier funnel domain models + tier→priority mapping."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import IntEnum

from pydantic import BaseModel, Field

from app.domain.enums import Direction
from app.providers.ratelimit import Priority


class Tier(IntEnum):
    BROAD = 1
    WATCHLIST = 2
    CANDIDATES = 3
    POSITIONS = 4


# Each tier claims API budget at a matching priority — open positions first,
# broad scan last (Phase 2 token bucket honors this under contention).
TIER_PRIORITY: dict[Tier, Priority] = {
    Tier.BROAD: Priority.BROAD,
    Tier.WATCHLIST: Priority.WATCHLIST,
    Tier.CANDIDATES: Priority.CANDIDATES,
    Tier.POSITIONS: Priority.POSITIONS,
}


def _now() -> datetime:
    return datetime.now(UTC)


class TierMember(BaseModel):
    symbol: str
    tier: Tier
    score: float = 0.0
    reason: str = ""
    metrics: dict[str, float] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=_now)


class Tier1Result(BaseModel):
    """Lightweight broad-universe evaluation (no chain, no flow)."""

    symbol: str
    passed: bool
    score: float
    gap_pct: float
    rel_volume: float
    has_catalyst: bool
    reasons: list[str] = Field(default_factory=list)


class Tier2Result(BaseModel):
    """Medium watchlist evaluation (flow + trend + IV, no chain)."""

    symbol: str
    score: float
    direction: Direction | None = None
    flow_score: float = 0.0
    price_score: float = 0.0
    vol_score: float = 0.0


class PositionRisk(BaseModel):
    """Tier-4 assessment of one open position."""

    symbol: str
    trade_id: str
    pnl_usd: float
    pnl_pct: float
    current_net: float
    dte: int | None = None
    action: str = "hold"  # hold | take_profit | stop | time_stop | expiry_risk
    note: str = ""
