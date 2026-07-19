"""short-duration paper trades table

Revision ID: 0003_short_duration_trades
Revises: 0002_short_duration
Create Date: 2026-07-19

Idempotent (created only if absent), safe over a create_all-bootstrapped DB.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_short_duration_trades"
down_revision: str | None = "0002_short_duration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("short_duration_trades"):
        return
    op.create_table(
        "short_duration_trades",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("candidate_id", sa.String(32), nullable=False),
        sa.Column("paper_trade_id", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("dte_category", sa.String(8), nullable=False),
        sa.Column("strategy", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("realized_pnl_usd", sa.Float(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_sd_trade_status_opened", "short_duration_trades", ["status", "opened_at"])
    op.create_index("ix_short_duration_trades_candidate_id", "short_duration_trades", ["candidate_id"])
    op.create_index("ix_short_duration_trades_paper_trade_id", "short_duration_trades", ["paper_trade_id"])


def downgrade() -> None:
    op.drop_table("short_duration_trades")
