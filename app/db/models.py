"""ORM models for persistence.

Design: keep the rich domain object as a JSON payload for traceability/replay,
while promoting the fields we filter and rank on to indexed columns. This keeps
migrations light while the domain models evolve.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String
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
