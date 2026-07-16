"""Scheduler process: periodic research scans during market hours.

Uses APScheduler's asyncio scheduler. A production deployment would gate on a
market-calendar provider; for now it runs on a fixed interval and logs results.
It NEVER places orders — it only produces and stores research.
"""

from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.logging_config import configure_logging, get_logger
from app.services import store
from app.services.scan_service import run_scan

log = get_logger(__name__)

SCAN_INTERVAL_MINUTES = 15


async def scheduled_scan() -> None:
    try:
        candidates = await run_scan()
        store.save_candidates(candidates)
        actionable = sum(c.is_actionable for c in candidates)
        log.info("scheduled_scan_ok", total=len(candidates), actionable=actionable)
    except Exception as exc:
        log.error("scheduled_scan_failed", error=str(exc))


async def main() -> None:
    configure_logging()
    scheduler = AsyncIOScheduler(timezone="America/New_York")
    scheduler.add_job(
        scheduled_scan,
        trigger=IntervalTrigger(minutes=SCAN_INTERVAL_MINUTES),
        id="research_scan",
        max_instances=1,
        coalesce=True,
        next_run_time=None,
    )
    scheduler.start()
    log.info("scheduler_started", interval_minutes=SCAN_INTERVAL_MINUTES)
    # Run one immediately, then idle forever.
    await scheduled_scan()
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
