"""Persistence for the multi-agent pipeline.

All tables are prefixed `ma_` and additive — no existing table is touched.
"""

from app.multiagent.db.models import ALL_TABLES
from app.multiagent.db.repository import (
    latest_runs,
    persist_scan,
    recommendations_for_run,
    record_decision,
    record_execution,
    record_result,
    save_scan,
    score_components_for,
)

__all__ = [
    "ALL_TABLES",
    "latest_runs",
    "persist_scan",
    "recommendations_for_run",
    "record_decision",
    "record_execution",
    "record_result",
    "save_scan",
    "score_components_for",
]
