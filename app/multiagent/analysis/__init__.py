"""Measurement layer: provider data in, `Measurement` objects out.

Nothing here applies a threshold or awards a point. Scoring reads these
measurements by name and grades them against `config/methodology.yaml`, so the
grading can change without touching what is measured — and so an audit can read
the measurements without reading the rubric.
"""

from app.multiagent.analysis.alignment import (
    bias_from_return,
    build_alignment_snapshot,
)
from app.multiagent.analysis.catalyst import horizon_days, validate_catalyst
from app.multiagent.analysis.contract_quality import build_contract_quality, build_risk_reward
from app.multiagent.analysis.flow import build_flow_snapshot
from app.multiagent.analysis.technical import (
    IndicatorContext,
    average_true_range,
    register,
    registered_indicators,
    run_indicators,
    swing_levels,
    trend_bias,
)

__all__ = [
    "IndicatorContext",
    "average_true_range",
    "bias_from_return",
    "build_alignment_snapshot",
    "build_contract_quality",
    "build_flow_snapshot",
    "build_risk_reward",
    "horizon_days",
    "register",
    "registered_indicators",
    "run_indicators",
    "swing_levels",
    "trend_bias",
    "validate_catalyst",
]
