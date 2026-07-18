"""ORM models for persistence.

Design: keep the rich domain object as a JSON payload for traceability/replay,
while promoting the fields we filter and rank on to indexed columns. This keeps
migrations light while the domain models evolve.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class ScanRow(Base, TimestampMixin):
    __tablename__ = "scans"

    scan_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    universe: Mapped[list] = mapped_column(JSON, nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    actionable_count: Mapped[int] = mapped_column(Integer, default=0)


class CandidateRow(Base, TimestampMixin):
    __tablename__ = "candidates"
    __table_args__ = (Index("ix_candidates_scan_score", "scan_id", "composite_score"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scan_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("scans.scan_id"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    composite_score: Mapped[float] = mapped_column(Float, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class ProposalRow(Base, TimestampMixin):
    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    scan_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    max_loss_usd: Mapped[float] = mapped_column(Float, nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class PaperTradeRow(Base, TimestampMixin):
    __tablename__ = "paper_trades"
    __table_args__ = (Index("ix_paper_symbol_status", "symbol", "status"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    scan_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    realized_pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe_usd: Mapped[float] = mapped_column(Float, default=0.0)
    mae_usd: Mapped[float] = mapped_column(Float, default=0.0)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class DecisionSnapshotRow(Base, TimestampMixin):
    """A frozen actionable decision — the warehouse of what we believed & when."""

    __tablename__ = "decision_snapshots"
    __table_args__ = (Index("ix_snap_symbol_gen", "symbol", "generated_at"),)

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scan_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy: Mapped[str] = mapped_column(String(24), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    composite_score: Mapped[float] = mapped_column(Float, nullable=False)
    probability_of_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_spot: Mapped[float] = mapped_column(Float, nullable=False)
    iv_rank: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_loss_usd: Mapped[float] = mapped_column(Float, nullable=False)
    expiration: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 'pending' until an outcome is recorded; promoted to 'resolved' after.
    resolution_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class DecisionOutcomeRow(Base, TimestampMixin):
    """A realized outcome for a decision at a given horizon (ground truth)."""

    __tablename__ = "decision_outcomes"
    __table_args__ = (Index("ix_outcome_decision_horizon", "decision_id", "horizon_label"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("decision_snapshots.decision_id"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    horizon_label: Mapped[str] = mapped_column(String(24), nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    direction_correct: Mapped[bool | None] = mapped_column(nullable=True)
    underlying_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_source: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class TierMemberRow(Base, TimestampMixin):
    """Current membership of a symbol in a funnel tier (Tier 1-4)."""

    __tablename__ = "tier_members"

    tier: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(String(128), default="")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


# --- Short-duration (0DTE / 1-5DTE) module ---


class ShortDurationCandidateRow(Base, TimestampMixin):
    """A short-duration trade candidate. Filter/rank fields promoted; the full
    domain object lives in `payload`."""

    __tablename__ = "short_duration_candidates"
    __table_args__ = (
        Index("ix_sd_cand_cat_state", "dte_category", "state"),
        Index("ix_sd_cand_detected", "detected_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    dte_category: Mapped[str] = mapped_column(String(8), nullable=False)
    strategy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class CandidateTransitionRow(Base, TimestampMixin):
    """Audit trail of a short-duration candidate's state changes."""

    __tablename__ = "candidate_state_transitions"
    __table_args__ = (Index("ix_sd_trans_cand_at", "candidate_id", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    from_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_state: Mapped[str] = mapped_column(String(16), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trigger: Mapped[str] = mapped_column(String(64), default="")
    actor: Mapped[str] = mapped_column(String(32), default="system")
    reason: Mapped[str] = mapped_column(String(256), default="")
    score_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class IntradayLevelsRow(Base, TimestampMixin):
    """Computed intraday session levels for one symbol on one session date."""

    __tablename__ = "intraday_levels"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    vwap: Mapped[float | None] = mapped_column(Float, nullable=True)
    opening_range_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    opening_range_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    relative_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class NewsItemRow(Base, TimestampMixin):
    """A news headline with full latency lineage, for the News page + analytics."""

    __tablename__ = "news_items"
    __table_args__ = (Index("ix_news_symbol_recv", "symbol", "received_ts"),)

    id: Mapped[str] = mapped_column(String(192), primary_key=True)
    symbol: Mapped[str | None] = mapped_column(String(16), nullable=True)
    headline: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="unknown")
    source_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duplicate_group_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class EventRestrictionRow(Base, TimestampMixin):
    """A trading-restricted window around a high-impact macro event."""

    __tablename__ = "event_restrictions"
    __table_args__ = (Index("ix_evt_restr_window", "window_start", "window_end"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_name: Mapped[str] = mapped_column(String(128), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trading_allowed: Mapped[bool] = mapped_column(default=False)
    size_modifier: Mapped[float] = mapped_column(Float, default=0.0)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
