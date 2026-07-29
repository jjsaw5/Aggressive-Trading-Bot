"""Shared performance primitives — one implementation for the backtest and the
forward-outcome scorecard, so "profit factor" and "drawdown" mean the same thing
everywhere they are reported.
"""

from __future__ import annotations


def max_drawdown(pnls_in_order: list[float]) -> float:
    """Largest peak-to-trough drop of the cumulative P&L curve, as a non-negative
    USD number. Inputs MUST be in chronological (resolution/close) order — a
    drawdown computed on unordered P&L is meaningless. Empty -> 0.0."""
    cum = peak = mdd = 0.0
    for p in pnls_in_order:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return round(mdd, 2)


def profit_factor(pnls: list[float]) -> float | None:
    """Gross wins / gross losses. None when there are no losses (undefined, not
    'infinite edge' — a no-loss sample cannot be graded)."""
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    return round(gross_win / gross_loss, 3) if gross_loss > 0 else None


def expectancy(pnls: list[float]) -> float | None:
    """Average P&L per trade (USD). None on an empty sample."""
    return round(sum(pnls) / len(pnls), 2) if pnls else None


def _ranks(xs: list[float]) -> list[float]:
    """Fractional (average) ranks, so ties share the mean of their positions."""
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


def spearman_ci(
    xs: list[float],
    ys: list[float],
    *,
    resamples: int = 2000,
    alpha: float = 0.05,
    min_n: int = 20,
    seed: int = 20260729,
) -> tuple[float, float] | None:
    """Percentile bootstrap confidence interval for the Spearman correlation.

    Exists because a point estimate near zero is indistinguishable from noise,
    and a gate that treats +0.03 as "discrimination" launders noise into
    permission. The interval answers the only question that matters: could this
    correlation plausibly be zero?

    Deterministic by construction (fixed seed) — a gate whose verdict flickers
    between runs on the same data is not a gate. Returns None when the base
    correlation is undefined or the sample is too small to bootstrap honestly."""
    import random

    base = spearman(xs, ys)
    if base is None or len(xs) < min_n:
        return None
    rng = random.Random(seed)
    n = len(xs)
    vals: list[float] = []
    for _ in range(resamples):
        idx = [rng.randrange(n) for _ in range(n)]
        s = spearman([xs[i] for i in idx], [ys[i] for i in idx])
        if s is not None:
            vals.append(s)
    if len(vals) < resamples // 2:  # too many degenerate resamples to trust
        return None
    vals.sort()
    lo = vals[int(alpha / 2 * len(vals))]
    hi = vals[min(len(vals) - 1, int((1 - alpha / 2) * len(vals)))]
    return round(lo, 4), round(hi, 4)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation in [-1, 1]. None when there are fewer than 3
    paired points or either side has zero rank variance (no orderable signal)."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None
    return round(cov / (vx**0.5 * vy**0.5), 4)
