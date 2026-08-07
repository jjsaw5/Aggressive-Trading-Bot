"""Agent 2's output contract: the ResearchCandidate.

Named `ResearchCandidate` rather than `TradeCandidate` because
`app.domain.candidates.TradeCandidate` already exists and means something else
(the deterministic swing scanner's output). Two classes with one name in one
codebase is how a mapping bug gets written; the spec's field list is honoured in
full, only the class name differs.

Per the spec there is deliberately **no 0-100 confidence score here**.
`preliminary_quality` is a coarse enum the agent uses to order its own ideas,
and it is not a term in the composite. The number comes later, from measurements.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from app.multiagent.models.enums import (
    Direction,
    StrategyType,
    TimeHorizon,
)


class PreliminaryQuality(str, Enum):
    """Agent 2's own ordering of its ideas. Not a score, not scored."""

    STRONG = "strong"
    MODERATE = "moderate"
    SPECULATIVE = "speculative"


class ExpectedMove(BaseModel):
    """The move the thesis needs, stated as a claim that can be checked.

    Both fields optional because an honest agent may have a direction without a
    magnitude. Downstream, a missing magnitude makes the breakeven-reachability
    rule ABSTAIN rather than pass or fail.
    """

    magnitude_pct: float | None = None
    direction_is_up: bool | None = None
    rationale: str = ""
    # Set by code from IV once the chain is available, for comparison against
    # the agent's claim. `NA_no_data` semantics: None means uncomputable.
    implied_move_pct: float | None = None


class SupportingCatalyst(BaseModel):
    summary: str
    evidence_refs: list[str] = Field(default_factory=list)


class ResearchCandidate(BaseModel):
    candidate_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    generated_at: datetime

    ticker: str
    direction: Direction
    strategy_type: StrategyType

    thesis: str
    primary_catalyst: str
    primary_catalyst_refs: list[str] = Field(default_factory=list)
    supporting_catalysts: list[SupportingCatalyst] = Field(default_factory=list)

    expected_holding_period: TimeHorizon = TimeHorizon.UNKNOWN
    expected_move: ExpectedMove = Field(default_factory=ExpectedMove)

    # The price the agent reasoned about. Filled by code from a real quote so
    # the thesis can later be checked against where the stock actually was.
    underlying_reference_price: float | None = None
    underlying_reference_as_of: datetime | None = None

    technical_context: str = ""
    invalidation_thesis: str = ""
    known_risks: list[str] = Field(default_factory=list)

    earnings_date: date | None = None
    catalyst_date: date | None = None

    preliminary_quality: PreliminaryQuality = PreliminaryQuality.MODERATE
    agent_reasoning_summary: str = ""

    # Provenance of the idea itself.
    evidence_refs: list[str] = Field(default_factory=list)
    dropped_claims: list[str] = Field(default_factory=list)

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    def all_refs(self) -> list[str]:
        refs = list(self.evidence_refs) + list(self.primary_catalyst_refs)
        for s in self.supporting_catalysts:
            refs.extend(s.evidence_refs)
        seen: dict[str, None] = {}
        for r in refs:
            seen.setdefault(r, None)
        return list(seen)

    def is_bullish(self) -> bool:
        return self.direction == Direction.BULLISH
