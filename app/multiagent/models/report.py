"""The ranked trade report and the human-decision records.

`RankedReport` is the run's deliverable: market summary, ranked trades with full
score breakdowns, and — equally important — the rejected candidates with their
reasons. The spec asks for rejections to be shown and stored, and that is not
politeness: the future performance engine's most valuable question is *"how
often did rejected trades actually work?"*, and it can only be answered if the
rejections were recorded at the time with the data that produced them.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.multiagent.models.brief import MarketBrief
from app.multiagent.models.candidates import ResearchCandidate
from app.multiagent.models.enums import (
    CalibrationStatus,
    Classification,
    DecisionAction,
    PipelineStage,
    RunStatus,
)
from app.multiagent.models.scoring import CompositeScore
from app.multiagent.models.validation import ValidationReport


class RankedTrade(BaseModel):
    """One recommendation, with everything needed to act or to audit."""

    rank: int
    candidate: ResearchCandidate
    validation: ValidationReport
    score: CompositeScore
    classification: Classification
    classification_name: str

    entry_conditions: list[str] = Field(default_factory=list)
    profit_targets: list[str] = Field(default_factory=list)
    invalidation: str = ""
    risks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RejectedTrade(BaseModel):
    """A candidate that did not qualify, and precisely why."""

    candidate: ResearchCandidate
    validation: ValidationReport | None = None
    score: CompositeScore | None = None
    classification: Classification = Classification.REJECT
    rejection_codes: list[str] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=list)
    # True when a hard rule fired, i.e. the rejection was terminal rather than
    # merely a low score. The report separates the two.
    hard_rejected: bool = False


class RunDiagnostics(BaseModel):
    """What the run could and could not see. Gaps are reported, not smoothed."""

    stage: PipelineStage
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    duration_seconds: float | None = None

    evidence_items: int = 0
    symbols_examined: int = 0
    candidates_generated: int = 0
    candidates_validated: int = 0

    provider_errors: dict[str, str] = Field(default_factory=dict)
    data_gaps: list[str] = Field(default_factory=list)
    dropped_agent_claims: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    agent_runner: str = "unknown"
    providers_used: list[str] = Field(default_factory=list)


class RankedReport(BaseModel):
    run_id: str
    generated_at: datetime
    methodology_version: str
    stage: PipelineStage
    calibration_status: CalibrationStatus = CalibrationStatus.UNCALIBRATED

    brief: MarketBrief
    ranked: list[RankedTrade] = Field(default_factory=list)
    rejected: list[RejectedTrade] = Field(default_factory=list)
    diagnostics: RunDiagnostics

    # Set when the run stopped before finalising contracts, e.g. a premarket
    # run. The report then presents theses without tradable structures and says
    # so rather than quoting a stale chain.
    contracts_finalised: bool = True
    stage_note: str = ""


class TradeDecision(BaseModel):
    """The human's call on a recommendation."""

    decision_id: str
    run_id: str
    candidate_id: str
    action: DecisionAction
    decided_at: datetime
    notes: str = ""


class TradeExecution(BaseModel):
    """What the human actually did, if they entered. Recorded, never placed.

    The system does not submit orders. This model exists so a manually-entered
    trade can be tied back to the recommendation that prompted it, which is the
    only way the future performance engine can compare score to outcome.
    """

    execution_id: str
    decision_id: str
    candidate_id: str

    entered_at: datetime
    contract_description: str
    quantity: int
    entry_price_per_contract: float
    underlying_price_at_entry: float | None = None

    stop_or_invalidation: str = ""
    target: str = ""
    notes: str = ""


class TradeResult(BaseModel):
    """The outcome. MFE/MAE are named as bounds, because that is what they are.

    CLAUDE.md §4: *"MFE/MAE come from bar extremes that have no ordering within
    the bar; they are not achieved prices."* The field names carry `_bound` so
    the caveat cannot be lost between here and a spreadsheet.
    """

    result_id: str
    execution_id: str
    candidate_id: str

    exited_at: datetime | None = None
    exit_price_per_contract: float | None = None
    realized_pnl: float | None = None

    max_favorable_excursion_bound: float | None = None
    max_adverse_excursion_bound: float | None = None
    excursion_note: str = (
        "MFE/MAE are bounds derived from bar extremes, not achieved prices. "
        "A bar that traded through both stop and target books as a loss."
    )

    underlying_price_at_exit: float | None = None
    notes: str = ""
