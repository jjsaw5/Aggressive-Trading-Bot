"""Ranked trade-candidate models — the primary Mode 1 (research) output."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.enums import CandidateStatus, Direction, RejectReason
from app.domain.signals import SignalScore
from app.domain.trades import TradePlan


class Thesis(BaseModel):
    """The narrative answer to the 14 platform questions, in structured form."""

    direction: Direction
    why_now: str
    flow_meaningful: bool
    price_confirms: bool
    has_catalyst: bool
    catalyst_note: str | None = None
    iv_favorable: bool
    iv_note: str | None = None
    invalidation: str  # what makes the thesis wrong


class TradeCandidate(BaseModel):
    symbol: str
    status: CandidateStatus = CandidateStatus.RANKED
    composite_score: float = Field(ge=0.0, le=1.0)
    direction: Direction
    thesis: Thesis
    signals: list[SignalScore] = Field(default_factory=list)
    trade_plan: TradePlan | None = None
    reject_reasons: list[RejectReason] = Field(default_factory=list)
    generated_at: datetime
    scan_id: str

    @property
    def is_actionable(self) -> bool:
        return self.status == CandidateStatus.RANKED and self.trade_plan is not None
