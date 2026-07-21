"""promote scoring-model + risk-policy version on short-duration candidates

Revision ID: 0004_sd_candidate_scoring_version
Revises: 0003_short_duration_trades
Create Date: 2026-07-21

Add-only + inspector-guarded: the columns are added only if the table exists and
the column is absent, so it is safe over a create_all-bootstrapped DB. The full
domain object (incl. scorecard weights) already lives in `payload`; these promoted
columns exist so a book can be filtered/compared by the version it was scored under.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_sd_candidate_scoring_version"
down_revision: str | None = "0003_short_duration_trades"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "short_duration_candidates"


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(_TABLE):
        return
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "scoring_model_version" not in cols:
        op.add_column(
            _TABLE,
            sa.Column("scoring_model_version", sa.String(48), nullable=False, server_default=""),
        )
    if "risk_policy_version" not in cols:
        op.add_column(
            _TABLE,
            sa.Column("risk_policy_version", sa.String(48), nullable=False, server_default=""),
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(_TABLE):
        return
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "risk_policy_version" in cols:
        op.drop_column(_TABLE, "risk_policy_version")
    if "scoring_model_version" in cols:
        op.drop_column(_TABLE, "scoring_model_version")
