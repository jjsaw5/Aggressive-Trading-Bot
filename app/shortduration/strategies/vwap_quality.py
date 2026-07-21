"""VWAP continuation-quality model.

A VWAP-trend continuation is only as good as the *quality* of the trend riding
VWAP — a clean, orderly push that pulls back to VWAP and holds is a very different
trade from a jagged tape that keeps losing and reclaiming the line. This module
turns that judgement into six named, inspectable sub-scores in [0,1] and a
weighted composite, so the strategy grades continuation quality instead of making
one all-or-nothing gate decision.

Sub-scores (all from the trade's perspective — bearish mirrors bullish):

- **continuation** — trend strength × cleanliness (slope magnitude and fit R²).
- **structure** — higher-highs/higher-lows (bull) consistency across the window.
- **vwap_hold** — fraction of closes on the correct side of VWAP, discounted by the
  depth of the worst violation.
- **pullback** — was there a *healthy* pullback toward VWAP (not absent/extended,
  not a deep breakdown)? Best when price approached VWAP and resumed.
- **volume** — expansion on the push relative to the pullback/early window.
- **controlled_reclaim** — if VWAP was lost, was it reclaimed in a shallow, brief,
  controlled way (vs a violent whipsaw or a trend that is still underwater)?

`controlled_reclaim` is what lets the model *accept a brief VWAP loss that was
cleanly reclaimed* instead of hard-rejecting it — while a real whipsaw still fails
on low vwap_hold + low reclaim quality.
"""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, Field

from app.domain.enums import Direction

# Composite weights (sum to 1.0). Continuation + structure carry the trend thesis;
# vwap_hold + controlled_reclaim carry the "rides the line" discipline; pullback and
# volume are confirmation. Internal to this model (not the per-DTE scoring weights).
_WEIGHTS: dict[str, float] = {
    "continuation": 0.25,
    "structure": 0.20,
    "vwap_hold": 0.20,
    "pullback": 0.15,
    "volume": 0.10,
    "controlled_reclaim": 0.10,
}
_TARGET_SLOPE_PCT = 0.002  # per-bar close slope (fraction of price) that scores full continuation


def _clamp01(x: float) -> float:
    return round(max(0.0, min(1.0, x)), 4)


class VWAPQuality(BaseModel):
    """Graded quality of a VWAP-trend continuation, with named sub-scores."""

    direction: Direction
    continuation: float
    structure: float
    vwap_hold: float
    pullback: float
    volume: float
    controlled_reclaim: float
    overall: float = 0.0
    slope_pct: float = 0.0
    notes: list[str] = Field(default_factory=list)

    def as_metadata(self) -> dict[str, float | str]:
        return {
            "vwap_continuation": self.continuation,
            "vwap_structure": self.structure,
            "vwap_hold": self.vwap_hold,
            "vwap_pullback": self.pullback,
            "vwap_volume": self.volume,
            "vwap_controlled_reclaim": self.controlled_reclaim,
            "vwap_quality": self.overall,
            "vwap_slope_pct": round(self.slope_pct, 6),
        }


def _structure_score(values: np.ndarray, bullish: bool) -> float:
    """Consistency of higher-highs/higher-lows (bull) across ~4 segments."""
    n = values.size
    if n < 4:
        return 0.5
    segs = np.array_split(values, 4)
    highs = [float(s.max()) for s in segs]
    lows = [float(s.min()) for s in segs]
    seq = highs if bullish else lows
    steps = len(seq) - 1
    if bullish:
        good = sum(1 for i in range(steps) if highs[i + 1] >= highs[i]) + \
               sum(1 for i in range(steps) if lows[i + 1] >= lows[i])
    else:
        good = sum(1 for i in range(steps) if lows[i + 1] <= lows[i]) + \
               sum(1 for i in range(steps) if highs[i + 1] <= highs[i])
    return _clamp01(good / (2 * steps))


def compute_vwap_quality(
    closes: np.ndarray, highs: np.ndarray, lows: np.ndarray, vols: np.ndarray,
    *, vwap: float, last: float, direction: Direction, slope_pct: float,
) -> VWAPQuality:
    bull = direction == Direction.BULLISH
    notes: list[str] = []
    price = last or float(closes[-1])

    # continuation: slope magnitude toward the trade × linear-fit cleanliness (R²).
    x = np.arange(closes.size, dtype=float)
    fit = np.polyfit(x, closes, 1)
    resid = closes - (fit[0] * x + fit[1])
    ss_res = float(np.sum(resid ** 2))
    ss_tot = float(np.sum((closes - closes.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    slope_strength = _clamp01(abs(slope_pct) / _TARGET_SLOPE_PCT)
    continuation = _clamp01(0.6 * slope_strength + 0.4 * _clamp01(r2))

    structure = _structure_score(highs if bull else lows, bull)

    # vwap_hold: fraction of closes on the right side, discounted by worst violation.
    on_side = (closes >= vwap) if bull else (closes <= vwap)
    frac = float(on_side.mean())
    worst = float((vwap - closes.min()) if bull else (closes.max() - vwap))
    worst_pct = max(0.0, worst) / price if price else 0.0
    vwap_hold = _clamp01(frac - min(0.4, worst_pct / 0.01 * 0.1))

    # pullback: did price come back toward VWAP (healthy) without breaking down?
    # distance of the closest approach to VWAP, as a fraction of the run's range.
    run_range = float(closes.max() - closes.min()) or price * 1e-4
    approach = float((closes.min() - vwap) if bull else (vwap - closes.max()))
    # approach>0 means never reached VWAP (extended); approach≈0 means kissed it.
    depth_below = max(0.0, -approach) / run_range   # went past VWAP by this much of range
    never_touched = max(0.0, approach) / run_range  # stayed this far above VWAP
    if depth_below > 0.6:
        pullback = _clamp01(0.4 - (depth_below - 0.6))
        notes.append("deep pullback through VWAP")
    elif never_touched > 0.8:
        pullback = 0.5  # straight run, no pullback yet — fine but unconfirmed
        notes.append("no pullback yet (extended)")
    else:
        pullback = _clamp01(1.0 - abs(0.15 - depth_below))  # best: a shallow tag of VWAP

    # volume: push (last third) vs early/pullback (first third).
    third = max(1, vols.size // 3)
    early = float(vols[:third].mean()) or 1.0
    late = float(vols[-third:].mean())
    volume = _clamp01(0.5 + 0.5 * ((late - early) / early))

    # controlled_reclaim: if VWAP was lost, reward a shallow, brief, reclaimed dip.
    lost = ~on_side
    if not bool(lost.any()):
        controlled_reclaim = 1.0  # never lost VWAP — nothing to reclaim, clean
    else:
        reclaimed = bool(on_side[-1])  # back on the right side now
        breaches = int(lost.sum())
        brevity = _clamp01(1.0 - breaches / max(1, closes.size))
        shallow = _clamp01(1.0 - worst_pct / 0.01)  # <1% excursion scores well
        controlled_reclaim = _clamp01((0.5 if reclaimed else 0.0) + 0.3 * shallow + 0.2 * brevity)
        if reclaimed:
            notes.append("controlled VWAP reclaim")
        else:
            notes.append("still below VWAP after loss")

    subs = {
        "continuation": continuation, "structure": structure, "vwap_hold": vwap_hold,
        "pullback": pullback, "volume": volume, "controlled_reclaim": controlled_reclaim,
    }
    overall = _clamp01(sum(subs[k] * w for k, w in _WEIGHTS.items()))
    return VWAPQuality(
        direction=direction, overall=overall, slope_pct=slope_pct, notes=notes, **subs
    )
