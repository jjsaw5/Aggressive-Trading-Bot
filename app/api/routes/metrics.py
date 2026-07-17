"""Observability endpoint — one aggregated snapshot of system health.

Pulls together the metrics registry (provider latency/requests/errors, funnel
timings, scheduler activity) with the subsystems' own stats (cache hit rate,
rate-limit buckets + budgets, event volume) and live tier membership sizes.
Read-only; safe to poll. The dashboard's Ops panel renders this.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.db import repository
from app.events.bus import get_event_bus
from app.observability.metrics import get_metrics
from app.providers.cache import get_response_cache
from app.providers.ratelimit import get_limiter

router = APIRouter(prefix="/metrics", tags=["metrics"])


class MetricsSnapshot(BaseModel):
    cache: dict
    rate_limit: dict
    events: dict
    tiers: dict
    registry: dict


@router.get("", response_model=MetricsSnapshot)
async def metrics_snapshot() -> MetricsSnapshot:
    members = await run_in_threadpool(repository.list_all_tiers)
    tier_sizes = {"broad": 0, "watchlist": 0, "candidates": 0, "positions": 0}
    name_by_tier = {1: "broad", 2: "watchlist", 3: "candidates", 4: "positions"}
    for m in members:
        tier_sizes[name_by_tier[int(m.tier)]] += 1

    return MetricsSnapshot(
        cache=get_response_cache().stats(),
        rate_limit=get_limiter().stats(),
        events=get_event_bus().stats(),
        tiers=tier_sizes,
        registry=get_metrics().snapshot(),
    )
