"""short-duration module tables + backfill of lagging warehouse tables

Revision ID: 0002_short_duration
Revises: 0001_initial
Create Date: 2026-07-18

Creates the 0DTE/1-5DTE module tables and, at the same time, closes the gap
where `decision_snapshots`, `decision_outcomes`, and `tier_members` were only
ever created by `create_all()` and never captured in a migration.

Every table is created only if absent, so this upgrade is safe on databases that
were bootstrapped via `create_all()` (the app's default) as well as on a fresh
alembic-managed database.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_short_duration"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = (
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)


def _has(table: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return insp.has_table(table)


def _create(table: str, *cols: sa.Column, indexes: list[tuple[str, list[str]]] | None = None) -> None:
    if _has(table):
        return
    op.create_table(table, *cols, *(_TS))
    for name, colnames in indexes or []:
        op.create_index(name, table, colnames)


def upgrade() -> None:
    # --- Backfill the warehouse tables that only create_all() had made ---
    _create(
        "decision_snapshots",
        sa.Column("decision_id", sa.String(64), primary_key=True),
        sa.Column("scan_id", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("strategy", sa.String(24), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("composite_score", sa.Float(), nullable=False),
        sa.Column("probability_of_profit", sa.Float(), nullable=True),
        sa.Column("entry_spot", sa.Float(), nullable=False),
        sa.Column("iv_rank", sa.Float(), nullable=True),
        sa.Column("max_loss_usd", sa.Float(), nullable=False),
        sa.Column("expiration", sa.Date(), nullable=True),
        sa.Column("resolution_status", sa.String(16), server_default="pending"),
        sa.Column("payload", sa.JSON(), nullable=False),
        indexes=[
            ("ix_snap_symbol_gen", ["symbol", "generated_at"]),
            ("ix_decision_snapshots_scan_id", ["scan_id"]),
            ("ix_decision_snapshots_resolution_status", ["resolution_status"]),
        ],
    )
    _create(
        "decision_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "decision_id",
            sa.String(64),
            sa.ForeignKey("decision_snapshots.decision_id"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("horizon_label", sa.String(24), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("direction_correct", sa.Boolean(), nullable=True),
        sa.Column("underlying_return_pct", sa.Float(), nullable=True),
        sa.Column("realized_pnl_usd", sa.Float(), nullable=True),
        sa.Column("outcome_source", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        indexes=[
            ("ix_outcome_decision_horizon", ["decision_id", "horizon_label"]),
            ("ix_decision_outcomes_symbol", ["symbol"]),
        ],
    )
    _create(
        "tier_members",
        sa.Column("tier", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(16), primary_key=True),
        sa.Column("score", sa.Float(), server_default="0"),
        sa.Column("reason", sa.String(128), server_default=""),
        sa.Column("payload", sa.JSON(), nullable=False),
    )

    # --- Short-duration module tables ---
    _create(
        "short_duration_candidates",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("dte_category", sa.String(8), nullable=False),
        sa.Column("strategy", sa.String(32), nullable=True),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("score", sa.Float(), server_default="0"),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        indexes=[
            ("ix_sd_cand_cat_state", ["dte_category", "state"]),
            ("ix_sd_cand_detected", ["detected_at"]),
            ("ix_short_duration_candidates_symbol", ["symbol"]),
        ],
    )
    _create(
        "candidate_state_transitions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("candidate_id", sa.String(32), nullable=False),
        sa.Column("from_state", sa.String(16), nullable=True),
        sa.Column("to_state", sa.String(16), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trigger", sa.String(64), server_default=""),
        sa.Column("actor", sa.String(32), server_default="system"),
        sa.Column("reason", sa.String(256), server_default=""),
        sa.Column("score_at", sa.Float(), nullable=True),
        indexes=[
            ("ix_sd_trans_cand_at", ["candidate_id", "at"]),
            ("ix_candidate_state_transitions_candidate_id", ["candidate_id"]),
        ],
    )
    _create(
        "intraday_levels",
        sa.Column("symbol", sa.String(16), primary_key=True),
        sa.Column("session_date", sa.Date(), primary_key=True),
        sa.Column("vwap", sa.Float(), nullable=True),
        sa.Column("opening_range_high", sa.Float(), nullable=True),
        sa.Column("opening_range_low", sa.Float(), nullable=True),
        sa.Column("relative_volume", sa.Float(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    _create(
        "news_items",
        sa.Column("id", sa.String(192), primary_key=True),
        sa.Column("symbol", sa.String(16), nullable=True),
        sa.Column("headline", sa.String(512), nullable=False),
        sa.Column("source", sa.String(64), server_default="unknown"),
        sa.Column("source_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duplicate_group_id", sa.String(64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        indexes=[
            ("ix_news_symbol_recv", ["symbol", "received_ts"]),
            ("ix_news_items_duplicate_group_id", ["duplicate_group_id"]),
        ],
    )
    _create(
        "event_restrictions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_name", sa.String(128), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trading_allowed", sa.Boolean(), server_default=sa.false()),
        sa.Column("size_modifier", sa.Float(), server_default="0"),
        sa.Column("payload", sa.JSON(), nullable=False),
        indexes=[("ix_evt_restr_window", ["window_start", "window_end"])],
    )


def downgrade() -> None:
    for table in (
        "event_restrictions",
        "news_items",
        "intraday_levels",
        "candidate_state_transitions",
        "short_duration_candidates",
    ):
        op.drop_table(table)
    # Leave the warehouse tables in place on downgrade — they predate this
    # migration in create_all-managed databases and other revisions assume them.