"""Scoring model re-exports.

The score models are pure pydantic and live in `app.domain.shortduration` so the
candidate can carry its scorecard without the domain layer depending on the
scoring package. This module re-exports them as the scoring package's public
surface.
"""

from __future__ import annotations

from app.domain.shortduration import (
    FactorScore,
    NewsScore,
    ScoreCard,
    ScoreComponent,
)

__all__ = ["FactorScore", "NewsScore", "ScoreCard", "ScoreComponent"]
