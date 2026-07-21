"""True market-internals model (distinct from watchlist participation).

Watchlist participation (% of *our* tracked universe above VWAP) is a proxy and
lives in `app/shortduration/breadth.py`. This model is for *real* market-wide
internals sourced from providers. Fields we cannot source on the current keys
(NYSE advance/decline issues, TICK, up/down volume, new highs/lows) are modeled
as `None` and listed in `unavailable_fields` — never silently defaulted to a
neutral/bullish value.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class MarketInternals(BaseModel):
    as_of: datetime
    source: str = "unknown"
    is_authoritative: bool = False  # True when at least one real internals signal is present

    # --- Sector breadth (real: FMP sector-performance-snapshot) ---
    sectors_total: int | None = None
    sectors_advancing: int | None = None
    sector_breadth_pct: float | None = None       # advancing / total, [0,1]
    avg_sector_change_pct: float | None = None

    # --- Options-flow tide (real: Unusual Whales market-tide) ---
    net_call_premium: float | None = None
    net_put_premium: float | None = None
    net_volume: float | None = None
    tide_direction: float | None = None            # [-1,1], + = call-premium heavy

    # --- Sector-flow breadth (real: UW sector-etfs) ---
    sectors_call_heavy: int | None = None
    sector_flow_total: int | None = None
    sector_flow_pct: float | None = None           # call-heavy sectors / total, [0,1]

    # --- Classic internals — NOT available on current provider keys ---
    advance_decline_issues: float | None = None
    tick: float | None = None
    up_down_volume_ratio: float | None = None
    new_highs_minus_lows: float | None = None

    unavailable_fields: list[str] = Field(default_factory=list)
    breadth_score: float | None = None             # [0,1] composite of available real signals
    errors: dict[str, str] = Field(default_factory=dict)

    @property
    def has_real_signal(self) -> bool:
        return self.breadth_score is not None
