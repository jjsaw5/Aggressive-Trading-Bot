"""Deterministic scoring: measurements in, an auditable 0-100 out.

No LLM output is an input to any arithmetic in this package.
"""

from app.multiagent.scoring.engine import classify, rank, score_candidate
from app.multiagent.scoring.rules import (
    abstain,
    band_rule,
    boolean_rule,
    penalty_rule,
    threshold_rule,
)

__all__ = [
    "abstain",
    "band_rule",
    "boolean_rule",
    "classify",
    "penalty_rule",
    "rank",
    "score_candidate",
    "threshold_rule",
]
