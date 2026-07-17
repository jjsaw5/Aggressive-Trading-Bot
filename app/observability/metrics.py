"""Lightweight in-process metrics: counters, gauges, and timing summaries.

No external dependency (no Prometheus client): a single registry the subsystems
write to and the /metrics endpoint reads. Timings keep a bounded sample window
and report count/avg/min/max/p95. Cheap enough to call on every request and
every tier run.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from contextlib import contextmanager
from functools import lru_cache


class Timing:
    __slots__ = ("count", "sum_ms", "min_ms", "max_ms", "_samples")

    def __init__(self) -> None:
        self.count = 0
        self.sum_ms = 0.0
        self.min_ms = float("inf")
        self.max_ms = 0.0
        self._samples: deque[float] = deque(maxlen=256)

    def observe(self, ms: float) -> None:
        self.count += 1
        self.sum_ms += ms
        self.min_ms = min(self.min_ms, ms)
        self.max_ms = max(self.max_ms, ms)
        self._samples.append(ms)

    def summary(self) -> dict:
        if not self.count:
            return {"count": 0}
        ordered = sorted(self._samples)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        return {
            "count": self.count,
            "avg_ms": round(self.sum_ms / self.count, 2),
            "min_ms": round(self.min_ms, 2),
            "max_ms": round(self.max_ms, 2),
            "p95_ms": round(p95, 2),
        }


class Metrics:
    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)
        self._gauges: dict[str, float] = {}
        self._timings: dict[str, Timing] = defaultdict(Timing)

    def inc(self, name: str, n: int = 1) -> None:
        self._counters[name] += n

    def set_gauge(self, name: str, value: float) -> None:
        self._gauges[name] = value

    def observe(self, name: str, ms: float) -> None:
        self._timings[name].observe(ms)

    @contextmanager
    def timer(self, name: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, (time.perf_counter() - t0) * 1000.0)

    def snapshot(self) -> dict:
        return {
            "counters": dict(sorted(self._counters.items())),
            "gauges": dict(sorted(self._gauges.items())),
            "timings": {k: self._timings[k].summary() for k in sorted(self._timings)},
        }

    def reset(self) -> None:
        self._counters.clear()
        self._gauges.clear()
        self._timings.clear()


@lru_cache
def get_metrics() -> Metrics:
    return Metrics()
