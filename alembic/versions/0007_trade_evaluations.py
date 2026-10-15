"""on-demand trade evaluations (human-proposed trade grading)

A separate table from `decision_snapshots` on purpose. Evaluations are grades of
trades the USER proposed, not signals the system generated; mixing them into the
capture corpus would let a user re-evaluating one idea forty times shift the base
rate the conviction gate is measured against.

Revision ID: 0007_trade_evaluations
Revises: 0006_daily_regimes
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007_trade_evaluations"
down_revision = "0006_daily_regimes"
branch_labels = None
depends_on = None

_TABLE = "trade_evaluations"


def upgrade() -> None:
    # Idempotent: the table may already exist where create_all ran first.
    if _TABLE in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("evaluation_id", sa.String(32), primary_key=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("structure", sa.String(24), nullable=False),
        sa.Column("requested_horizon", sa.String(16), nullable=False),
        sa.Column("resolved_expiration", sa.Date(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        # Nullable: an evaluation that could assess nothing is a real result.
        # A NOT NULL here would force an `or 0.0` at the call site, which is the
        # exact defect that made 67 of 67 audited signals report a zero spot.
        sa.Column("grade", sa.String(2), nullable=True),
        sa.Column("composite", sa.Float(), nullable=True),
        sa.Column("dimensions_assessed", sa.Integer(), nullable=True),
        sa.Column("probability_of_profit", sa.Float(), nullable=True),
        sa.Column("max_loss_usd", sa.Float(), nullable=True),
        sa.Column("graded", sa.String(16), nullable=False, server_default="proposed"),
        sa.Column("evaluator_version", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_eval_symbol_created", _TABLE, ["symbol", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_eval_symbol_created", table_name=_TABLE)
    op.drop_table(_TABLE)
