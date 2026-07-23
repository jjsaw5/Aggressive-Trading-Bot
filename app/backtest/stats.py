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


def _ranks(xs: list[float]) -> list[float]:
    """Fractional ranks (ties averaged) — the basis for Spearman."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # 1-based average rank across the tie block
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None  # a constant column has no defined correlation
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return sxy / (sxx**0.5 * syy**0.5)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation. None if <3 points or either side is constant."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    return _pearson(_ranks(xs), _ranks(ys))


@dataclass(frozen=True)
class CorrCI:
    point: float  # Spearman rho over the paired sample
    lo: float
    hi: float
    n: int

    @property
    def excludes_zero(self) -> bool:
        return self.lo > 0 or self.hi < 0


def bootstrap_corr_ci(
    xs: list[float], ys: list[float], *, iters: int = 5000, seed: int = 12345, alpha: float = 0.05
) -> CorrCI | None:
    """Percentile bootstrap CI for the Spearman rho of paired (x, y). Deterministic
    (seeded). None if the base correlation is undefined."""
    point = spearman(xs, ys)
    if point is None:
        return None
    rng = random.Random(seed)
    n = len(xs)
    rhos: list[float] = []
    for _ in range(iters):
        idx = [rng.randrange(n) for _ in range(n)]
        rho = spearman([xs[i] for i in idx], [ys[i] for i in idx])
        if rho is not None:
            rhos.append(rho)
    if len(rhos) < 100:
        return None
    rhos.sort()
    lo = rhos[int((alpha / 2) * len(rhos))]
    hi = rhos[int((1 - alpha / 2) * len(rhos)) - 1]
    return CorrCI(point=round(point, 4), lo=round(lo, 4), hi=round(hi, 4), n=n)


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
