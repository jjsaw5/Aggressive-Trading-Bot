"""Signal models — the intermediate scored evidence behind a candidate.

Each analyzer emits a `SignalScore` in [0, 1] plus human-readable rationale so
every candidate can answer *why now* and *is the flow meaningful*.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.enums import Direction


class SignalScore(BaseModel):
    name: str  # e.g. "options_flow", "price_action", "volatility"
    score: float = Field(ge=0.0, le=1.0)  # 0 = no edge, 1 = strong edge
    direction: Direction | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    rationale: str = ""
    details: dict[str, float | str | bool | None] = Field(default_factory=dict)


class SignalBundle(BaseModel):
    """All signal scores computed for one symbol during a scan."""

    symbol: str
    scores: list[SignalScore] = Field(default_factory=list)

    def by_name(self, name: str) -> SignalScore | None:
        return next((s for s in self.scores if s.name == name), None)
