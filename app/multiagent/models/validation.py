"""Agent 3's output contract: the ValidationReport.

The division of labour inside Agent 3 is the point of the whole design:

* the **snapshots** below (`TechnicalSnapshot`, `FlowSnapshot`, ...) are
  measured by Python from provider responses. No LLM writes a number here.
* the **verdicts and narratives** are the agent's skeptical reading of those
  measurements, and they are recorded and shown but never summed.

So an LLM can tell you it thinks the flow is being misread; it cannot move a
point of the score by saying so.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from app.multiagent.models.contracts import ProposedStructure
from app.multiagent.models.enums import (
    BiasDirection,
    EvidenceQuality,
    ValidationVerdict,
)
from app.multiagent.models.measurements import MeasurementSet


class TechnicalSnapshot(BaseModel):
    """Price structure, measured.

    The indicator set is deliberately open: `measurements` is a bag, and
    `app.multiagent.analysis.technical` registers indicators into it. Adding an
    indicator is registering a function, not editing this model — which is what
    "design the technical framework so indicators can be added or modified
    easily" asks for.
    """

    symbol: str
    as_of: datetime
    source: str = "unknown"

    price: float | None = None
    prev_close: float | None = None

    measurements: MeasurementSet = Field(default_factory=MeasurementSet)

    # Named levels, kept separate from scalar measurements because a report
    # renders them differently.
    support_levels: list[float] = Field(default_factory=list)
    resistance_levels: list[float] = Field(default_factory=list)

    trend_bias: BiasDirection = BiasDirection.UNKNOWN
    notes: list[str] = Field(default_factory=list)
    # Bar count actually available. A 14-period ATR from 9 bars is not an ATR.
    bars_available: int = 0


class MarketAlignmentSnapshot(BaseModel):
    symbol: str
    as_of: datetime

    spy_bias: BiasDirection = BiasDirection.UNKNOWN
    qqq_bias: BiasDirection = BiasDirection.UNKNOWN
    sector: str | None = None
    sector_proxy: str | None = None
    sector_bias: BiasDirection = BiasDirection.UNKNOWN

    measurements: MeasurementSet = Field(default_factory=MeasurementSet)

    aligned_with_spy: bool | None = None
    aligned_with_qqq: bool | None = None
    aligned_with_sector: bool | None = None
    fighting_the_tape: bool | None = None
    notes: list[str] = Field(default_factory=list)


class CatalystValidation(BaseModel):
    """Whether the reason for the trade survives contact with the record."""

    ticker: str
    as_of: datetime

    claimed_catalyst: str
    exists: bool | None = None            # resolves against retrieved evidence
    resolved_evidence_ids: list[str] = Field(default_factory=list)
    unresolved_refs: list[str] = Field(default_factory=list)

    newest_evidence_age_days: float | None = None
    evidence_quality: EvidenceQuality = EvidenceQuality.INTERPRETATION
    is_scheduled: bool = False
    scheduled_date: date | None = None
    within_expected_horizon: bool | None = None

    # Move since the catalyst published; the "already priced in" question.
    move_since_catalyst_pct: float | None = None
    likely_priced_in: bool | None = None

    conflicting_events: list[str] = Field(default_factory=list)
    verdict: ValidationVerdict = ValidationVerdict.INSUFFICIENT_DATA
    notes: list[str] = Field(default_factory=list)


class FlowSnapshot(BaseModel):
    """Options flow, measured and deliberately not over-read.

    The spec is explicit: *"The system must NOT assume that every large options
    transaction is bullish or bearish."* So this model records the ambiguity —
    `direction_ambiguous` is set whenever the call/put split is near even or the
    at-ask share is unknown, and the scoring rule abstains rather than guessing.
    """

    symbol: str
    as_of: datetime
    lookback_hours: int
    alerts_considered: int = 0

    measurements: MeasurementSet = Field(default_factory=MeasurementSet)

    call_premium: float | None = None
    put_premium: float | None = None
    net_premium: float | None = None            # calls minus puts
    ask_side_premium: float | None = None
    bid_side_premium: float | None = None
    sweep_count: int = 0
    largest_print_premium: float | None = None

    # Volume/OI relationship: size above OI implies a new position rather than
    # a closing one. `None` when OI was not supplied — not zero.
    max_size_over_oi: float | None = None
    likely_opening: bool | None = None

    top_strikes: list[float] = Field(default_factory=list)
    concentration_ratio: float | None = None    # top strike premium / total

    implied_bias: BiasDirection = BiasDirection.UNKNOWN
    direction_ambiguous: bool = True
    interpretation: str = ""
    caveats: list[str] = Field(default_factory=list)
    verdict: ValidationVerdict = ValidationVerdict.INSUFFICIENT_DATA


class ContractQualitySnapshot(BaseModel):
    """Tradability of the chosen structure."""

    structure_id: str
    as_of: datetime

    measurements: MeasurementSet = Field(default_factory=MeasurementSet)

    worst_spread_pct: float | None = None
    min_open_interest: int | None = None
    min_volume: int | None = None
    iv: float | None = None
    iv_rank: float | None = None
    iv_percentile: float | None = None
    iv_source: str | None = None
    term_structure_slope: float | None = None

    liquidity_acceptable: bool | None = None
    notes: list[str] = Field(default_factory=list)


class RiskRewardSnapshot(BaseModel):
    """What the trade risks, needs and pays — all from the priced structure."""

    structure_id: str
    as_of: datetime

    measurements: MeasurementSet = Field(default_factory=MeasurementSet)

    max_loss: float | None = None
    max_profit: float | None = None
    breakeven: float | None = None
    breakeven_move_pct: float | None = None
    reward_to_risk: float | None = None
    target_reward_to_risk: float | None = None   # for unbounded long options
    expected_move_pct: float | None = None       # IV-implied over the hold
    distance_to_invalidation_pct: float | None = None
    theta_burden: float | None = None            # theta over hold / debit paid
    iv_crush_exposure: float | None = None       # vega x plausible IV drop / debit
    event_risk_notes: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ValidationReport(BaseModel):
    """Everything Agent 3 established about one candidate."""

    candidate_id: str
    run_id: str
    ticker: str
    validated_at: datetime
    stage: str = "market_open"

    technical: TechnicalSnapshot | None = None
    alignment: MarketAlignmentSnapshot | None = None
    catalyst: CatalystValidation | None = None
    flow: FlowSnapshot | None = None
    contract_quality: ContractQualitySnapshot | None = None
    risk_reward: RiskRewardSnapshot | None = None

    structures: list[ProposedStructure] = Field(default_factory=list)
    selected_structure_id: str | None = None

    # The skeptic's summary. Displayed prominently, never scored.
    overall_verdict: ValidationVerdict = ValidationVerdict.INSUFFICIENT_DATA
    disconfirming_findings: list[str] = Field(default_factory=list)
    confirming_findings: list[str] = Field(default_factory=list)
    agent_commentary: str = ""

    data_gaps: list[str] = Field(default_factory=list)
    provider_errors: dict[str, str] = Field(default_factory=dict)

    def selected_structure(self) -> ProposedStructure | None:
        if self.selected_structure_id is None:
            return self.structures[0] if self.structures else None
        for s in self.structures:
            if s.structure_id == self.selected_structure_id:
                return s
        return None
