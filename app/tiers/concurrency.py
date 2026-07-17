"""Bounded concurrency for tier fan-out.

Tier 1 may sweep hundreds of symbols; issuing all requests at once would swamp
the providers. `bounded_gather` caps in-flight work with a semaphore while the
Phase-2 token bucket enforces the actual per-provider rate. A failing task is
isolated (logged, returned as None) so one bad symbol never kills the sweep.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from app.logging_config import get_logger

log = get_logger(__name__)

T = TypeVar("T")


async def bounded_gather(coros: list[Awaitable[T]], *, limit: int = 8) -> list[T | None]:
    sem = asyncio.Semaphore(max(1, limit))

    async def _run(coro: Awaitable[T]) -> T | None:
        async with sem:
            try:
                return await coro
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the sweep
                log.warning("tier_task_failed", error=str(exc))
                return None

    return await asyncio.gather(*(_run(c) for c in coros))
