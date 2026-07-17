"""Scheduler process: periodic research scans.

Uses APScheduler's asyncio scheduler. A production deployment would gate on a
market-calendar provider; for now it runs on a fixed interval and logs results.
It NEVER places orders — it only produces and stores research.

The cadence is configurable via SCAN_INTERVAL_MINUTES (default 180 = every 3
hours). Because the scanner is not yet market-session-aware and runs 24/7, a
slow baseline keeps closed-market API waste low until per-tier session-aware
cadences land.
"""

from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.alerts.service import alert_candidates
from app.config import settings
from app.db import repository
from app.db.session import create_all
from app.engine.universe import UniverseConfig
from app.logging_config import configure_logging, get_logger
from app.services.events_service import publish_scan_events
from app.services.outcomes_service import resolve_pending, warehouse_candidates
from app.services.scan_service import run_scan

log = get_logger(__name__)


async def scheduled_scan() -> None:
    try:
        candidates = await run_scan()
        actionable = sum(c.is_actionable for c in candidates)
        if candidates:
            await asyncio.to_thread(
                repository.save_scan,
                candidates[0].scan_id,
                UniverseConfig().normalized_symbols(),
                candidates,
            )
            await warehouse_candidates(candidates)
            # Detect material change vs the previous scan and publish events.
            await publish_scan_events(candidates)
            await alert_candidates(candidates)
        # Resolve any decisions that have matured against current prices.
        try:
            await resolve_pending(min_age_days=1)
        except Exception as exc:
            log.warning("resolve_pending_failed", error=str(exc))
        log.info("scheduled_scan_ok", total=len(candidates), actionable=actionable)
    except Exception as exc:
        log.error("scheduled_scan_failed", error=str(exc))


async def main() -> None:
    configure_logging()
    try:
        await asyncio.to_thread(create_all)
    except Exception as exc:
        log.warning("db_init_skipped", error=str(exc))
    interval = settings.scan_interval_minutes
    scheduler = AsyncIOScheduler(timezone="America/New_York")
    scheduler.add_job(
        scheduled_scan,
        trigger=IntervalTrigger(minutes=interval),
        id="research_scan",
        max_instances=1,
        coalesce=True,
        next_run_time=None,
    )
    scheduler.start()
    log.info("scheduler_started", interval_minutes=interval)
    # Run one immediately, then idle forever.
    await scheduled_scan()
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
