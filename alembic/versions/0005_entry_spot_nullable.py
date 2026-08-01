"""make decision_snapshots.entry_spot nullable

Revision ID: 0005_entry_spot_nullable
Revises: 0004_sd_candidate_scoring_version
Create Date: 2026-07-31

Audit finding B1: `entry_spot` read 0.0 on all 67 scanner rows of the signal
export while the data dictionary claimed the field was available. The column was
NOT NULL, so every builder that could not obtain a spot wrote `or 0.0` — a silent
null wearing a plausible price. `plan.analytics` is a funnel-lineage object and
is None for short-duration plans, so that fallback fired on every scanner row.

Absence must be representable, otherwise the type system forces the corruption.
Widening only: a NOT NULL column becoming nullable accepts every value it
accepted before, so this cannot fail on existing data. SQLite (and libsql) cannot
ALTER a column's nullability in place, so the batch operation rebuilds the table;
other backends take the direct ALTER path.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_entry_spot_nullable"
down_revision: str | None = "0004_sd_candidate_scoring_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "decision_snapshots"
_COL = "entry_spot"


def _has_column() -> bool:
    """Guarded so the migration is safe over a create_all-bootstrapped DB, where
    the table may already carry the widened definition."""
    insp = sa.inspect(op.get_bind())
    if _TABLE not in insp.get_table_names():
        return False
    return any(c["name"] == _COL for c in insp.get_columns(_TABLE))


def upgrade() -> None:
    if not _has_column():
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.alter_column(_COL, existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    # Narrowing back would fail on any row written since — a NULL spot has no
    # honest numeric substitute, and inventing 0.0 is the bug this reverses.
    # Backfilling is a data decision, not a schema one, so it is left to a human.
    if not _has_column():
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.alter_column(_COL, existing_type=sa.Float(), nullable=False)
