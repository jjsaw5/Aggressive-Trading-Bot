"""Walk-forward splitting with purge + embargo (experiment spec §4.3/§4.4).

Option lifetimes overlap, so a naive time split leaks: a trade that resolves after
the train cutoff can carry information from the test period back into training.
This splits the timeline into folds where the train set is only trades **fully
resolved** at least `embargo_days` before the test window opens — purging
straddlers and embargoing the gap. Supports both walk directions (train-early /
test-late and the reverse) for the same-sign robustness check.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass
class Fold:
    index: int
    train: list
    test: list
    test_start: date
    test_end: date


def _cuts(dates: list[date], n_folds: int) -> list[date]:
    lo, hi = dates[0], dates[-1]
    span = (hi - lo).days
    step = max(1, span // (n_folds + 1))
    return [lo + timedelta(days=step * i) for i in range(1, n_folds + 1)] + [hi + timedelta(days=1)]


def walk_forward_folds(
    items: list,
    entry_of: Callable[[object], date],
    exit_of: Callable[[object], date],
    *,
    n_folds: int,
    embargo_days: int,
    reverse: bool = False,
) -> list[Fold]:
    """Expanding-window folds. Test segment i = items entering in [cut_i, cut_{i+1}).
    Train = items fully resolved >= embargo before the test opens (or, in reverse,
    entering >= embargo after the test closes). One fold per test segment."""
    if len(items) < 2:
        return []
    entries = sorted({entry_of(it) for it in items})
    cuts = _cuts(entries, n_folds)
    folds: list[Fold] = []
    for i in range(n_folds):
        t_start, t_end = cuts[i], cuts[i + 1]
        test = [it for it in items if t_start <= entry_of(it) < t_end]
        if not reverse:
            barrier = t_start - timedelta(days=embargo_days)
            train = [it for it in items if exit_of(it) <= barrier]
        else:
            barrier = t_end + timedelta(days=embargo_days)
            train = [it for it in items if entry_of(it) >= barrier]
        if test and train:
            folds.append(Fold(index=i, train=train, test=test, test_start=t_start, test_end=t_end))
    return folds
