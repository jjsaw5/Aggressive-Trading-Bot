"""daily market-regime table (pre-flight P6)

Market-level regime, one row per session, so the pre-registration's per-regime
cuts group by a class that exists independently of any signal. Reviewer Ruling 2
rejected substituting the per-signal vol x tape tag for this.

Revision ID: 0006_daily_regimes
Revises: 0005_entry_spot_nullable
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006_daily_regimes"
down_revision = "0005_entry_spot_nullable"
branch_labels = None
depends_on = None

_TABLE = "daily_regimes"


def upgrade() -> None:
    # Idempotent: the table may already exist where create_all ran first.
    if _TABLE in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("session", sa.Date(), primary_key=True),
        sa.Column("vix_close", sa.Float(), nullable=True),
        sa.Column("vix_percentile_20d", sa.Float(), nullable=True),
        sa.Column("spx_realized_vol_20d", sa.Float(), nullable=True),
        sa.Column("spx_vs_50d_sma", sa.Float(), nullable=True),
        sa.Column("regime_class", sa.String(24), nullable=False),
        sa.Column("vol_state", sa.String(12), nullable=False),
        sa.Column("trend_state", sa.String(12), nullable=False),
        sa.Column("source", sa.String(24), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The per-regime gate groups by this across the whole corpus.
    op.create_index(f"ix_{_TABLE}_regime_class", _TABLE, ["regime_class"])


def downgrade() -> None:
    op.drop_index(f"ix_{_TABLE}_regime_class", table_name=_TABLE)
    op.drop_table(_TABLE)
