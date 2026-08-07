"""Persistence schema for the multi-agent research pipeline.

All tables are prefixed `ma_` and are additive: nothing here touches the
existing platform schema, and no existing table is altered. That matters under
`CLAUDE.md` §2 — the capture window permits data persistence, and keeping this
subsystem's storage physically separate means a migration here can never change
what the frozen short-duration model reads.

**The shape, and why.** Indexed columns carry what queries filter and sort on;
the full domain object is stored alongside as a JSON `payload`. The platform
already uses this pattern (`app/db/models.py`) and the reason applies doubly
here: the whole point of the corpus is to answer questions nobody has asked yet
("which catalysts perform best?", "does flow improve results?"), and a fully
normalised schema would have to guess those questions in advance. The payload
means a recommendation can always be replayed exactly as it was produced.

Every table that records a claim also records where the claim came from and when
it was retrieved, because the future performance engine's questions are all of
the form "given what was known at time T, what happened next?"
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class MARunRow(Base, TimestampMixin):
    """One pipeline run. Everything else hangs off `run_id`."""

    __tablename__ = "ma_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    methodology_version: Mapped[str] = mapped_column(String(64), nullable=False)
    scoring_model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    # "deterministic" or "anthropic:<model>". A corpus mixing the two must be
    # separable, so this is an indexed column rather than a payload field.
    agent_runner: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    trading_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    # Recorded on every row so the corpus can demonstrate that no run ever had
    # execution enabled, rather than the claim resting on a document.
    execution_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    contracts_finalised: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stage_note: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class MAMarketBriefRow(Base, TimestampMixin):
    """Agent 1's output, one per run."""

    __tablename__ = "ma_market_briefs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("ma_runs.run_id"), index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    market_regime: Mapped[str] = mapped_column(String(24), index=True)
    volatility_regime: Mapped[str] = mapped_column(String(24), index=True)
    spy_bias: Mapped[str] = mapped_column(String(16))
    qqq_bias: Mapped[str] = mapped_column(String(16))
    # Nullable, never zero-filled: a run with no VIX quote stores NULL.
    vix_level: Mapped[float | None] = mapped_column(Float)
    relevance_confidence: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class MAEconomicEventRow(Base, TimestampMixin):
    __tablename__ = "ma_economic_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("ma_runs.run_id"), index=True)
    evidence_id: Mapped[str | None] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    catalyst_type: Mapped[str] = mapped_column(String(40), index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    importance: Mapped[str] = mapped_column(String(16))
    consensus: Mapped[float | None] = mapped_column(Float)
    previous: Mapped[float | None] = mapped_column(Float)
    actual: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(64), default="unknown")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class MANewsItemRow(Base, TimestampMixin):
    """A retrieved news item with full provenance.

    `published_at` and `retrieved_at` are both stored and are different
    questions. A row with a NULL `published_at` is undated, which is not the
    same as fresh.
    """

    __tablename__ = "ma_news_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("ma_runs.run_id"), index=True)
    evidence_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(96), nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    catalyst_type: Mapped[str] = mapped_column(String(40), index=True)
    scope: Mapped[str] = mapped_column(String(16))
    evidence_quality: Mapped[str] = mapped_column(String(24))
    relevance_confidence: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class MAStockCatalystRow(Base, TimestampMixin):
    __tablename__ = "ma_stock_catalysts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("ma_runs.run_id"), index=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    catalyst_type: Mapped[str] = mapped_column(String(40), index=True)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(96))
    source_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_direction: Mapped[str] = mapped_column(String(16), index=True)
    importance: Mapped[str] = mapped_column(String(16))
    importance_score: Mapped[float | None] = mapped_column(Float)
    expected_time_horizon: Mapped[str] = mapped_column(String(16))
    scheduled_event_date: Mapped[datetime | None] = mapped_column(Date)
    evidence_quality: Mapped[str] = mapped_column(String(24), index=True)
    scope: Mapped[str] = mapped_column(String(16))
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class MACandidateRow(Base, TimestampMixin):
    """Agent 2's hypothesis. Deliberately carries no score column."""

    __tablename__ = "ma_trade_candidates"

    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("ma_runs.run_id"), index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    strategy_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    thesis: Mapped[str] = mapped_column(Text, default="")
    primary_catalyst: Mapped[str] = mapped_column(Text, default="")
    expected_holding_period: Mapped[str] = mapped_column(String(16), index=True)
    expected_move_pct: Mapped[float | None] = mapped_column(Float)
    underlying_reference_price: Mapped[float | None] = mapped_column(Float)
    invalidation_thesis: Mapped[str] = mapped_column(Text, default="")
    earnings_date: Mapped[datetime | None] = mapped_column(Date)
    catalyst_date: Mapped[datetime | None] = mapped_column(Date)
    preliminary_quality: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class MAValidationRow(Base, TimestampMixin):
    __tablename__ = "ma_trade_validations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    candidate_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ma_trade_candidates.candidate_id"), index=True
    )
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stage: Mapped[str] = mapped_column(String(24))
    overall_verdict: Mapped[str] = mapped_column(String(24), index=True)
    catalyst_verdict: Mapped[str | None] = mapped_column(String(24))
    flow_verdict: Mapped[str | None] = mapped_column(String(24))
    selected_structure_id: Mapped[str | None] = mapped_column(String(64), index=True)
    agent_commentary: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class MATechnicalSnapshotRow(Base, TimestampMixin):
    """Price structure as measured at decision time, for replay."""

    __tablename__ = "ma_technical_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    price: Mapped[float | None] = mapped_column(Float)
    trend_bias: Mapped[str] = mapped_column(String(16))
    bars_available: Mapped[int] = mapped_column(Integer, default=0)
    # Measurements keep their absence reasons, so a replay can tell "not
    # measured" from "measured as zero".
    measurements: Mapped[dict] = mapped_column(JSON, default=dict)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class MAOptionsFlowSnapshotRow(Base, TimestampMixin):
    __tablename__ = "ma_options_flow_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    alerts_considered: Mapped[int] = mapped_column(Integer, default=0)
    call_premium: Mapped[float | None] = mapped_column(Float)
    put_premium: Mapped[float | None] = mapped_column(Float)
    net_premium: Mapped[float | None] = mapped_column(Float)
    ask_side_premium: Mapped[float | None] = mapped_column(Float)
    sweep_count: Mapped[int] = mapped_column(Integer, default=0)
    implied_bias: Mapped[str] = mapped_column(String(16))
    # The honest field. True means the data did not support a directional read.
    direction_ambiguous: Mapped[bool] = mapped_column(Boolean, default=True)
    verdict: Mapped[str] = mapped_column(String(24), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class MAOptionContractSnapshotRow(Base, TimestampMixin):
    """A priced structure exactly as quoted when it was selected."""

    __tablename__ = "ma_option_contract_snapshots"

    structure_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    strategy_type: Mapped[str] = mapped_column(String(32), index=True)
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    expiration: Mapped[datetime | None] = mapped_column(Date, index=True)
    underlying_price: Mapped[float | None] = mapped_column(Float)
    net_debit_per_share: Mapped[float | None] = mapped_column(Float)
    contracts: Mapped[int] = mapped_column(Integer, default=0)
    max_loss: Mapped[float | None] = mapped_column(Float)
    max_profit: Mapped[float | None] = mapped_column(Float)
    breakeven: Mapped[float | None] = mapped_column(Float)
    reward_to_risk: Mapped[float | None] = mapped_column(Float)
    worst_leg_spread_pct: Mapped[float | None] = mapped_column(Float)
    min_open_interest: Mapped[int | None] = mapped_column(Integer)
    min_volume: Mapped[int | None] = mapped_column(Integer)
    net_delta: Mapped[float | None] = mapped_column(Float)
    net_theta: Mapped[float | None] = mapped_column(Float)
    net_vega: Mapped[float | None] = mapped_column(Float)
    # "provider" or "modeled". Stored because a modeled Greek and an observed
    # one must never be compared as though they were the same measurement.
    greeks_source: Mapped[str] = mapped_column(String(16), default="provider")
    probability_of_profit: Mapped[float | None] = mapped_column(Float)
    cost_drag_pct: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class MAScoreComponentRow(Base, TimestampMixin):
    """One scored category, with its rules in the payload.

    A row per category rather than a wide table so a new category does not
    require a migration, and so the per-rule audit trail travels with it.
    """

    __tablename__ = "ma_score_components"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    points_awarded: Mapped[float] = mapped_column(Float, nullable=False)
    points_available: Mapped[float] = mapped_column(Float, nullable=False)
    normalized: Mapped[float | None] = mapped_column(Float)
    abstained: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    coverage: Mapped[float | None] = mapped_column(Float)
    rules: Mapped[list] = mapped_column(JSON, default=list)


class MARecommendationRow(Base, TimestampMixin):
    """The composite score and disposition. What ranking and analytics read."""

    __tablename__ = "ma_trade_recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    candidate_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ma_trade_candidates.candidate_id"), index=True
    )
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    strategy_type: Mapped[str] = mapped_column(String(32), index=True)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    methodology_version: Mapped[str] = mapped_column(String(64), index=True)

    score: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    raw_points: Mapped[float] = mapped_column(Float, nullable=False)
    measured_weight: Mapped[float] = mapped_column(Float, nullable=False)
    input_coverage: Mapped[float] = mapped_column(Float, nullable=False)
    classification: Mapped[str] = mapped_column(String(24), index=True)
    # UNCALIBRATED until a feature clears out-of-sample validation. Indexed so a
    # future analysis can never accidentally pool calibrated and uncalibrated rows.
    calibration_status: Mapped[str] = mapped_column(String(16), index=True)

    is_ranked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    rank: Mapped[int | None] = mapped_column(Integer)
    hard_rejected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    rejection_codes: Mapped[list] = mapped_column(JSON, default=list)
    rejection_reasons: Mapped[list] = mapped_column(JSON, default=list)
    structure_id: Mapped[str | None] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class MADecisionRow(Base, TimestampMixin):
    """The human's call. Nothing in this system writes it automatically."""

    __tablename__ = "ma_trade_decisions"

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class MAExecutionRow(Base, TimestampMixin):
    """A trade the human entered manually.

    The system does not place orders. This records what a person did, so a
    recommendation can be tied to an outcome.
    """

    __tablename__ = "ma_trade_executions"

    execution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(64), ForeignKey("ma_trade_decisions.decision_id"), index=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    contract_description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price_per_contract: Mapped[float] = mapped_column(Float, nullable=False)
    underlying_price_at_entry: Mapped[float | None] = mapped_column(Float)
    stop_or_invalidation: Mapped[str] = mapped_column(Text, default="")
    target: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")


class MAResultRow(Base, TimestampMixin):
    """Outcome. MFE/MAE are named as bounds because that is what they are."""

    __tablename__ = "ma_trade_results"

    result_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str] = mapped_column(String(64), ForeignKey("ma_trade_executions.execution_id"), index=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    exited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    exit_price_per_contract: Mapped[float | None] = mapped_column(Float)
    realized_pnl: Mapped[float | None] = mapped_column(Float, index=True)
    max_favorable_excursion_bound: Mapped[float | None] = mapped_column(Float)
    max_adverse_excursion_bound: Mapped[float | None] = mapped_column(Float)
    underlying_price_at_exit: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str] = mapped_column(Text, default="")


class MAAgentRunRow(Base, TimestampMixin):
    """One agent invocation. Prompts and responses are stored redacted."""

    __tablename__ = "ma_agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("ma_runs.run_id"), index=True)
    agent: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(24))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), index=True)
    runner: Mapped[str] = mapped_column(String(64), index=True)
    definition_path: Mapped[str | None] = mapped_column(Text)
    prompt_excerpt: Mapped[str] = mapped_column(Text, default="")
    raw_response_excerpt: Mapped[str] = mapped_column(Text, default="")
    structured_output: Mapped[dict | None] = mapped_column(JSON)
    tools_used: Mapped[list] = mapped_column(JSON, default=list)
    providers_queried: Mapped[list] = mapped_column(JSON, default=list)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    missing_data: Mapped[list] = mapped_column(JSON, default=list)
    validation_warnings: Mapped[list] = mapped_column(JSON, default=list)
    dropped_claims: Mapped[list] = mapped_column(JSON, default=list)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)


class MAProviderRequestRow(Base, TimestampMixin):
    """One outbound data call. No URL, no headers — those carry credentials."""

    __tablename__ = "ma_data_provider_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("ma_runs.run_id"), index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    capability: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    error: Mapped[str | None] = mapped_column(Text)
    result_count: Mapped[int | None] = mapped_column(Integer)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)


class MADataQualityFlagRow(Base, TimestampMixin):
    """Every gap, disagreement, stale reading and dropped agent claim."""

    __tablename__ = "ma_data_quality_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("ma_runs.run_id"), index=True)
    flag: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Composite indexes for the questions the performance engine will actually ask.
Index("ix_ma_reco_score_class", MARecommendationRow.score, MARecommendationRow.classification)
Index("ix_ma_reco_ticker_run", MARecommendationRow.ticker, MARecommendationRow.run_id)
Index("ix_ma_score_comp_cat_run", MAScoreComponentRow.category, MAScoreComponentRow.run_id)
Index("ix_ma_catalyst_ticker_type", MAStockCatalystRow.ticker, MAStockCatalystRow.catalyst_type)


ALL_TABLES = (
    MARunRow,
    MAMarketBriefRow,
    MAEconomicEventRow,
    MANewsItemRow,
    MAStockCatalystRow,
    MACandidateRow,
    MAValidationRow,
    MATechnicalSnapshotRow,
    MAOptionsFlowSnapshotRow,
    MAOptionContractSnapshotRow,
    MAScoreComponentRow,
    MARecommendationRow,
    MADecisionRow,
    MAExecutionRow,
    MAResultRow,
    MAAgentRunRow,
    MAProviderRequestRow,
    MADataQualityFlagRow,
)
