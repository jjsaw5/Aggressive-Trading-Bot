"""Startup schema self-heal: create_all must ADD promoted columns to an existing
table, not just create missing tables (the deploy has no separate migration step)."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from app.db.base import Base
from app.db.session import _sync_columns


def _cols(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_sync_adds_missing_promoted_columns(tmp_path) -> None:
    import app.db.models  # noqa: F401  register the tables on Base.metadata

    engine = create_engine(f"sqlite:///{tmp_path/'legacy.db'}", future=True)
    # Simulate a pre-v2 table: short_duration_candidates WITHOUT the Phase-2 promoted
    # version columns, and with a row already in it (the ALTER must survive that).
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE short_duration_candidates ("
            "id VARCHAR PRIMARY KEY, symbol VARCHAR, dte_category VARCHAR, direction VARCHAR, "
            "state VARCHAR, score FLOAT, detected_at DATETIME, payload JSON NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO short_duration_candidates "
            "(id, symbol, dte_category, direction, state, score, detected_at, payload) "
            "VALUES ('x', 'SPY', '0dte', 'bullish', 'detected', 0.5, '2026-07-17', '{}')"
        ))

    before = _cols(engine, "short_duration_candidates")
    assert "scoring_model_version" not in before

    _sync_columns(Base, engine)

    after = _cols(engine, "short_duration_candidates")
    assert {"scoring_model_version", "risk_policy_version"} <= after
    # Existing row is intact and the new NOT NULL column got its safe default.
    with engine.begin() as conn:
        v = conn.execute(text(
            "SELECT scoring_model_version FROM short_duration_candidates WHERE id='x'"
        )).scalar()
    assert v == ""

    # Idempotent: a second pass is a no-op (no error, no duplicate column).
    _sync_columns(Base, engine)
    assert {"scoring_model_version", "risk_policy_version"} <= _cols(engine, "short_duration_candidates")
