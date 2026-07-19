"""Dedicated short-duration control loop.

A separate fast loop (NOT a 5th scan tier) so sub-15s position monitoring never
head-of-line-blocks behind the core funnel's broad sweep. It runs only when
`SHORT_DURATION_ENABLED` and only during regular trading hours, at independent
cadences: monitor open positions most often (capital at risk), then the 0DTE and
1–5DTE scans. `due_jobs` is pure/testable; `tick` runs the due jobs best-effort so
one failure never stalls the others. It never places a live order.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from app.config import settings
from app.domain.enums import DTECategory
from app.logging_config import get_logger
from app.observability.metrics import get_metrics
from app.providers.ratelimit import Priority, use_priority
from app.scheduling.clock import MarketClock

log = get_logger(__name__)

_JOBS = ("monitor", "scan_0dte", "scan_1_5dte")


class ShortDurationLoop:
    def __init__(self, *, clock: MarketClock | None = None, tick_seconds: int | None = None) -> None:
        self.clock = clock or MarketClock()
        self.tick_seconds = tick_seconds or settings.short_duration_loop_tick_seconds
        self._last: dict[str, float] = {}
        self._running = False

    def _cadence(self, job: str) -> int:
        return {
            "monitor": settings.short_duration_monitor_seconds,
            "scan_0dte": settings.short_duration_scan_0dte_seconds,
            "scan_1_5dte": settings.short_duration_scan_1_5dte_seconds,
        }[job]

    def due_jobs(self, mono: float) -> list[str]:
        """Jobs whose cadence has elapsed (monitor first — highest priority)."""
        due = []
        for job in _JOBS:
            last = self._last.get(job)
            if last is None or (mono - last) >= self._cadence(job):
                due.append(job)
        return due

    async def _run_job(self, job: str, now: datetime) -> None:
        # Imported lazily so the loop module stays import-light for tests.
        from app.shortduration.detection import run_detection
        from app.shortduration.paper import monitor_short_duration_positions

        if job == "monitor":
            with use_priority(Priority.POSITIONS):
                await monitor_short_duration_positions(now=now)
        elif job == "scan_0dte":
            await run_detection(DTECategory.ZERO_DTE, now=now)
        elif job == "scan_1_5dte":
            await run_detection(DTECategory.SHORT_DTE, now=now)

    async def tick(self, *, now: datetime | None = None, mono: float | None = None) -> list[str]:
        now = now or datetime.now(UTC)
        mono = mono if mono is not None else time.monotonic()
        if not self.clock.is_market_open(now):
            return []  # RTH only — no closed-market API waste
        metrics = get_metrics()
        ran: list[str] = []
        for job in self.due_jobs(mono):
            try:
                with metrics.timer(f"sd_loop.{job}.ms"):
                    await self._run_job(job, now)
            except Exception as exc:  # noqa: BLE001 - a bad job can't stall the loop
                log.warning("sd_loop_job_failed", job=job, error=str(exc))
                metrics.inc(f"sd_loop.{job}.failures")
            self._last[job] = mono
            metrics.inc(f"sd_loop.{job}.runs")
            ran.append(job)
        return ran

    async def run_forever(self) -> None:
        self._running = True
        log.info(
            "sd_loop_started",
            tick_seconds=self.tick_seconds,
            monitor=settings.short_duration_monitor_seconds,
        )
        while self._running:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - never exit on a tick error
                log.error("sd_loop_tick_failed", error=str(exc))
            await asyncio.sleep(self.tick_seconds)

    def stop(self) -> None:
        self._running = False
