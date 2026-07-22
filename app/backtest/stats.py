"""Small statistics for the flow experiment: bootstrap CI on a mean difference,
and multiple-comparisons bookkeeping over the threshold grid (spec §4.5/§4.7).

Deterministic: the bootstrap is seeded so a run is reproducible.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class DiffCI:
    point: float  # mean(a) − mean(b)
    lo: float  # 2.5th percentile
    hi: float  # 97.5th percentile
    n_a: int
    n_b: int

    @property
    def excludes_zero(self) -> bool:
        return self.lo > 0 or self.hi < 0


def bootstrap_diff_ci(
    a: list[float], b: list[float], *, iters: int = 10000, seed: int = 12345, alpha: float = 0.05
) -> DiffCI | None:
    """Percentile bootstrap CI for mean(a) − mean(b). None if either arm is empty."""
    if not a or not b:
        return None
    rng = random.Random(seed)
    na, nb = len(a), len(b)
    diffs = []
    for _ in range(iters):
        ma = sum(a[rng.randrange(na)] for _ in range(na)) / na
        mb = sum(b[rng.randrange(nb)] for _ in range(nb)) / nb
        diffs.append(ma - mb)
    diffs.sort()
    lo = diffs[int((alpha / 2) * iters)]
    hi = diffs[int((1 - alpha / 2) * iters) - 1]
    return DiffCI(point=round(sum(a) / na - sum(b) / nb, 4), lo=round(lo, 4), hi=round(hi, 4), n_a=na, n_b=nb)


def bonferroni_alpha(alpha: float, m: int) -> float:
    """Family-wise per-comparison alpha over m comparisons."""
    return alpha / max(1, m)


@dataclass(frozen=True)
class GridSummary:
    n_cells: int
    median_lift: float | None  # median OOS CONFIRM−OPPOSE spread across the grid
    frac_positive: float | None  # share of grid cells with a positive OOS spread
    best_lift: float | None  # argmax (reported but NOT the verdict)


def summarize_grid(lifts: list[float]) -> GridSummary:
    """Distribution of out-of-sample lifts across the threshold grid — the median,
    not the argmax, is what the spec reports (§4.5)."""
    if not lifts:
        return GridSummary(0, None, None, None)
    s = sorted(lifts)
    n = len(s)
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    return GridSummary(
        n_cells=n,
        median_lift=round(median, 4),
        frac_positive=round(sum(1 for x in s if x > 0) / n, 4),
        best_lift=round(s[-1], 4),
    )
