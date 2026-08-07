"""Agent 1's output contract: the MarketBrief.

Every claim-bearing model here carries `evidence_refs`. That field is not
decoration — `app.multiagent.agents.market_intelligence` drops any element whose
refs do not resolve against the run's ledger. A catalyst with no evidence does
not reach Agent 2.

Note what is deliberately absent: there is no numeric confidence that feeds
scoring. `importance` is an enum the agent picks and `relevance_confidence` is
its own stated confidence; both are recorded and shown, and neither is a term in
the composite score. Numbers that matter come from providers.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from app.multiagent.models.enums import (
    BiasDirection,
    CatalystScope,
    CatalystType,
    EvidenceQuality,
    ExpectedDirection,
    Importance,
    MarketRegime,
    TimeHorizon,
    VolatilityRegime,
)


class SourceReference(BaseModel):
    """A citation, resolved from the ledger. Never author-supplied prose."""

    evidence_id: str
    source: str
    url: str | None = None
    headline: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime


class NewsReference(BaseModel):
    """A news item Agent 1 considered material, bound to retrieved evidence."""

    evidence_id: str
    ticker: str | None = None
    headline: str
    source: str
    url: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime
    catalyst_type: CatalystType = CatalystType.OTHER
    scope: CatalystScope = CatalystScope.COMPANY
    relevance_confidence: float = Field(0.5, ge=0.0, le=1.0)
    why_relevant: str = ""


class MacroEvent(BaseModel):
    """A macro release or policy event, scheduled or already out."""

    name: str
    catalyst_type: CatalystType
    scheduled_at: datetime | None = None
    is_scheduled: bool = True
    importance: Importance = Importance.MEDIUM
    expected_direction: ExpectedDirection = ExpectedDirection.UNKNOWN
    # Absent stays absent: a release with no consensus published reads None, not 0.
    consensus: float | None = None
    previous: float | None = None
    actual: float | None = None
    affected_markets: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    notes: str = ""


class SectorObservation(BaseModel):
    sector: str
    bias: BiasDirection = BiasDirection.UNKNOWN
    # Measured by code from the sector proxy's price history where available.
    trailing_return_pct: float | None = None
    proxy_symbol: str | None = None
    rationale: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class CompanyCatalyst(BaseModel):
    """The per-ticker catalyst record the spec calls for."""

    ticker: str
    catalyst_type: CatalystType
    headline: str
    description: str = ""
    source: str
    source_url: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime | None = None

    expected_direction: ExpectedDirection = ExpectedDirection.UNKNOWN
    # The agent's own importance call, 0-1. Recorded, displayed, and used only
    # to ORDER catalysts — never summed into the composite score.
    importance_score: float = Field(0.5, ge=0.0, le=1.0)
    importance: Importance = Importance.MEDIUM
    expected_time_horizon: TimeHorizon = TimeHorizon.UNKNOWN

    scheduled_event_date: date | None = None
    is_scheduled: bool = False
    evidence_quality: EvidenceQuality = EvidenceQuality.INTERPRETATION
    scope: CatalystScope = CatalystScope.COMPANY

    evidence_refs: list[str] = Field(default_factory=list)

    def is_evidenced(self) -> bool:
        """Whether anything retrieved actually backs this catalyst."""
        return bool(self.evidence_refs)


class RiskEvent(BaseModel):
    """Something that could invalidate positioning during the horizon."""

    name: str
    description: str = ""
    scheduled_at: datetime | None = None
    importance: Importance = Importance.MEDIUM
    affected_symbols: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class IndexContext(BaseModel):
    """Measured index state. Populated by code, not by an agent."""

    symbol: str
    price: float | None = None
    change_pct: float | None = None
    trailing_20d_return_pct: float | None = None
    above_20d_sma: bool | None = None
    above_50d_sma: bool | None = None
    bias: BiasDirection = BiasDirection.UNKNOWN
    as_of: datetime | None = None
    source: str = "unknown"


class VIXContext(BaseModel):
    level: float | None = None
    change_pct: float | None = None
    regime: VolatilityRegime = VolatilityRegime.UNKNOWN
    # Where the level came from; None when no provider supplied one.
    source: str | None = None
    as_of: datetime | None = None
    commentary: str = ""


class MarketBrief(BaseModel):
    """Agent 1's structured answer.

    Measured fields (`spy`, `qqq`, `iwm`, `vix.level`) are filled by code from
    provider data before the agent runs, and the agent may not overwrite them.
    Interpretive fields (`market_regime`, `summary`, catalyst classification)
    are the agent's, bound to evidence.
    """

    run_id: str
    generated_at: datetime
    methodology_version: str = ""
    stage: str = "premarket"

    market_regime: MarketRegime = MarketRegime.UNKNOWN
    volatility_regime: VolatilityRegime = VolatilityRegime.UNKNOWN

    spy: IndexContext | None = None
    qqq: IndexContext | None = None
    iwm: IndexContext | None = None
    vix: VIXContext = Field(default_factory=VIXContext)

    spy_bias: BiasDirection = BiasDirection.UNKNOWN
    qqq_bias: BiasDirection = BiasDirection.UNKNOWN

    macro_events: list[MacroEvent] = Field(default_factory=list)
    upcoming_scheduled_events: list[MacroEvent] = Field(default_factory=list)
    sector_observations: list[SectorObservation] = Field(default_factory=list)
    company_catalysts: list[CompanyCatalyst] = Field(default_factory=list)
    news_items: list[NewsReference] = Field(default_factory=list)
    risk_events: list[RiskEvent] = Field(default_factory=list)

    source_references: list[SourceReference] = Field(default_factory=list)

    # The agent's confidence that the brief is a relevant read of conditions.
    # Displayed; never scored.
    relevance_confidence: float = Field(0.5, ge=0.0, le=1.0)
    summary: str = ""

    # Gaps are as informative as the data (CLAUDE.md §4).
    data_gaps: list[str] = Field(default_factory=list)
    dropped_claims: list[str] = Field(default_factory=list)

    def catalysts_for(self, ticker: str) -> list[CompanyCatalyst]:
        t = ticker.upper()
        return [c for c in self.company_catalysts if c.ticker.upper() == t]

    def tickers_with_catalysts(self) -> list[str]:
        seen: dict[str, None] = {}
        for c in self.company_catalysts:
            seen.setdefault(c.ticker.upper(), None)
        return list(seen)
