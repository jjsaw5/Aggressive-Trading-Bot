"""Short-duration (0DTE / 1-5DTE) domain models.

Pure pydantic, no I/O. Freshness and data-lineage are first-class: every model
that comes from a feed carries timestamps so staleness can be detected and never
silently treated as neutral. Classification/scoring fields are Optional — they
are populated in later phases (news scoring, flow decay, data-quality), and their
absence is meaningful (not-yet-scored), not zero.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, computed_field

from app.domain.enums import (
    CandidateState,
    Direction,
    DTECategory,
    ShortDurationRegime,
    ShortDurationStrategy,
)


class IntradayBar(BaseModel):
    """One intraday OHLCV bar (1-min or 5-min)."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class IntradayLevels(BaseModel):
    """Session levels computed from intraday bars for one symbol.

    All prices Optional because early in the session (or with missing data) not
    every level exists yet — e.g. the opening range isn't set until the OR window
    closes. `None` means "not established", never zero.
    """

    symbol: str
    session_date: date
    last: float | None = None
    vwap: float | None = None
    opening_range_high: float | None = None
    opening_range_low: float | None = None
    opening_range_minutes: int | None = None
    premarket_high: float | None = None
    premarket_low: float | None = None
    prior_day_high: float | None = None
    prior_day_low: float | None = None
    relative_volume: float | None = None  # session volume vs typical to-this-point
    computed_at: datetime
    source: str = "unknown"

    @property
    def above_vwap(self) -> bool | None:
        if self.last is None or self.vwap is None:
            return None
        return self.last > self.vwap

    @property
    def above_opening_range(self) -> bool | None:
        if self.last is None or self.opening_range_high is None:
            return None
        return self.last > self.opening_range_high

    @property
    def below_opening_range(self) -> bool | None:
        if self.last is None or self.opening_range_low is None:
            return None
        return self.last < self.opening_range_low


class NewsItem(BaseModel):
    """A single news headline with full latency lineage.

    Latency timestamps are the point of this model: end-to-end delay from the
    event to the alert is what makes or breaks short-duration news trades, so we
    store every hop. Classification fields (novelty..flow_confirmation) are set
    by the news engine in a later phase.
    """

    id: str
    symbol: str | None = None
    headline: str
    summary: str = ""
    source: str = "unknown"
    category: str | None = None
    url: str | None = None

    # Latency lineage (UTC). source_ts = when the outlet published;
    # provider_ts = when the data vendor made it available; received_ts = when we
    # pulled it; parsed_ts/candidate_ts/alert_ts filled as it flows downstream.
    source_ts: datetime | None = None
    provider_ts: datetime | None = None
    received_ts: datetime
    parsed_ts: datetime | None = None
    candidate_ts: datetime | None = None
    alert_ts: datetime | None = None

    # Classification (Phase 3). None = not yet scored.
    novelty: float | None = None
    materiality: float | None = None
    direction: Direction | None = None
    expected_duration: str | None = None
    relevance: float | None = None
    source_quality: float | None = None
    price_confirmed: bool | None = None
    volume_confirmed: bool | None = None
    flow_confirmed: bool | None = None
    duplicate_group_id: str | None = None
    raw_ref: str | None = None  # pointer/hash to the retained raw payload

    @computed_field  # type: ignore[prop-decorator]
    @property
    def end_to_end_latency_s(self) -> float | None:
        """Seconds from source publication to our receipt, when both known.
        A computed field so it is serialized in API responses (the News page
        renders it directly)."""
        if self.source_ts is None:
            return None
        return round((self.received_ts - self.source_ts).total_seconds(), 3)


class EconomicEvent(BaseModel):
    """A scheduled macro release (CPI, FOMC, NFP, ...)."""

    name: str
    category: str | None = None
    country: str | None = None
    scheduled_at: datetime
    impact: str | None = None  # "low" | "medium" | "high"
    previous: float | None = None
    consensus: float | None = None
    actual: float | None = None
    affected_markets: list[str] = Field(default_factory=list)
    status: str = "scheduled"  # scheduled | released
    source: str = "unknown"

    def minutes_until(self, now: datetime) -> float:
        return round((self.scheduled_at - now).total_seconds() / 60.0, 1)


class EventRestriction(BaseModel):
    """A trading-restricted window around a high-impact event."""

    event_name: str
    window_start: datetime
    window_end: datetime
    affected_symbols: list[str] = Field(default_factory=list)  # empty = all
    affected_strategies: list[ShortDurationStrategy] = Field(default_factory=list)
    size_modifier: float = 0.0  # 0 = no new trades; 0.5 = half size; 1 = normal
    trading_allowed: bool = False

    def covers(self, now: datetime, symbol: str) -> bool:
        if not (self.window_start <= now <= self.window_end):
            return False
        return not self.affected_symbols or symbol.upper() in {
            s.upper() for s in self.affected_symbols
        }


class RegimeFactor(BaseModel):
    """One supporting or contradicting input to the regime call."""

    name: str
    value: str
    supports: bool


class ShortDurationRegimeState(BaseModel):
    """Intraday market-regime assessment for the banner and gating.

    `allow_new_trades` and `reduce_size` are the operative outputs — the regime
    engine can veto or throttle entries independent of any single candidate.
    """

    regime: ShortDurationRegime
    confidence: float  # [0, 1]
    factors: list[RegimeFactor] = Field(default_factory=list)
    allow_new_trades: bool = True
    reduce_size: bool = False
    next_event_name: str | None = None
    next_event_minutes: float | None = None
    # Component readings (for transparency; None when the input was unavailable).
    spy_trend_pct: float | None = None
    qqq_trend_pct: float | None = None
    iwm_trend_pct: float | None = None
    breadth_above_vwap_pct: float | None = None
    vol_reading: float | None = None
    as_of: datetime
    notes: str = ""

    @property
    def supporting(self) -> list[RegimeFactor]:
        return [f for f in self.factors if f.supports]

    @property
    def contradicting(self) -> list[RegimeFactor]:
        return [f for f in self.factors if not f.supports]


class ContractRecommendation(BaseModel):
    """A recommended contract or defined-risk structure for a candidate.

    Phase 1 leaves most fields empty (no detection yet); Phase 4 fills them from
    the short-duration contract-selection engine."""

    description: str = ""
    strategy: ShortDurationStrategy | None = None
    legs: list[dict] = Field(default_factory=list)
    max_loss_usd: float | None = None
    max_profit_usd: float | None = None
    breakevens: list[float] = Field(default_factory=list)
    est_fill_net: float | None = None
    liquidity_note: str = ""


class ShortDurationCandidate(BaseModel):
    """A short-duration trade candidate with full explanation and lifecycle.

    The candidate names a confirmed setup first; the option expression is a
    property of it. Detection/scoring land in later phases — in Phase 1 this
    model backs the read-only boards and the state machine, populated by a
    context-only scan stub.
    """

    id: str
    symbol: str
    dte_category: DTECategory
    strategy: ShortDurationStrategy | None = None
    direction: Direction = Direction.NEUTRAL
    detected_at: datetime
    regime: ShortDurationRegime | None = None
    score: float = 0.0  # [0, 1]
    confidence: float = 0.0
    entry_trigger: str = ""
    invalidation: str = ""
    targets: list[float] = Field(default_factory=list)
    contract: ContractRecommendation | None = None
    max_risk_usd: float | None = None
    catalyst: str | None = None
    state: CandidateState = CandidateState.DETECTED
    data_quality_score: float | None = None
    reasons: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    # Scoring (Phase 3). scorecard carries the full explainable breakdown.
    scorecard: ScoreCard | None = None
    news_score: NewsScore | None = None


class CandidateTransition(BaseModel):
    """An audit record of one candidate state change."""

    candidate_id: str
    from_state: CandidateState | None
    to_state: CandidateState
    at: datetime
    trigger: str = ""
    actor: str = "system"  # "system" | a user id
    reason: str = ""
    score_at: float | None = None


# --- Scoring models (pure; the scoring engine populates them) ----------------


class FactorScore(BaseModel):
    """One weighted factor in a DTE scoring model."""

    key: str
    label: str
    weight: float  # points out of 100
    raw: float  # [0,1] quality of this factor
    points: float  # raw * weight
    explanation: str = ""


class ScoreComponent(BaseModel):
    """A named sub-score in [0,1] with its rationale. `None` = input unavailable
    (unknown), never zero."""

    value: float | None = None
    explanation: str = ""


class NewsScore(BaseModel):
    """Weighted news classification (source authority .. flow confirmation)."""

    total: float  # [0,1]
    source_authority: float
    novelty: float
    materiality: float
    relevance: float
    price_confirmed: float
    volume_confirmed: float
    flow_confirmed: float
    is_duplicate: bool = False
    direction: Direction | None = None
    explanation: str = ""


class ScoreCard(BaseModel):
    """Full, explainable score for a short-duration candidate."""

    dte_category: str
    total: float  # [0,100]
    overall_confidence: float  # [0,1] — total tempered by data quality
    factors: list[FactorScore] = Field(default_factory=list)
    components: dict[str, ScoreComponent] = Field(default_factory=dict)
    data_quality: float = 0.0  # [0,1]
    summary: str = ""

    @property
    def normalized(self) -> float:
        return round(self.total / 100.0, 4)
