"""initial schema: scans, candidates, proposals, paper_trades

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-16
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scans",
        sa.Column("scan_id", sa.String(32), primary_key=True),
        sa.Column("universe", sa.JSON(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), server_default="0"),
        sa.Column("actionable_count", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "candidates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scan_id", sa.String(32), sa.ForeignKey("scans.scan_id"), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("composite_score", sa.Float(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_candidates_symbol", "candidates", ["symbol"])
    op.create_index("ix_candidates_scan_score", "candidates", ["scan_id", "composite_score"])

    op.create_table(
        "proposals",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("scan_id", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("max_loss_usd", sa.Float(), nullable=False),
        sa.Column("approved_by", sa.String(64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_proposals_scan_id", "proposals", ["scan_id"])
    op.create_index("ix_proposals_symbol", "proposals", ["symbol"])

    op.create_table(
        "paper_trades",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("scan_id", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("realized_pnl_usd", sa.Float(), nullable=True),
        sa.Column("mfe_usd", sa.Float(), server_default="0"),
        sa.Column("mae_usd", sa.Float(), server_default="0"),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_paper_trades_scan_id", "paper_trades", ["scan_id"])
    op.create_index("ix_paper_symbol_status", "paper_trades", ["symbol", "status"])


def downgrade() -> None:
    op.drop_table("paper_trades")
    op.drop_table("proposals")
    op.drop_table("candidates")
    op.drop_table("scans")
