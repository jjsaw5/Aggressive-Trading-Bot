"""Decision-warehouse service: capture decisions and resolve their outcomes.

- `warehouse_candidates` freezes every actionable candidate into a
  `DecisionSnapshot` at scan time (the moment of decision).
- `resolve_pending` later marks decisions against reality: it fetches the
  current underlying quote through the provider abstraction and scores each
  eligible pending decision via the breakeven proxy.

DB work is offloaded to threads (the persistence layer is synchronous/libSQL);
provider calls are async.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.analytics.outcomes import resolve_underlying
from app.analytics.snapshots import snapshot_from_candidate
from app.db import repository
from app.domain.candidates import TradeCandidate
from app.domain.outcomes import DecisionOutcome, DecisionSource
from app.logging_config import get_logger
from app.providers import registry

log = get_logger(__name__)


async def warehouse_candidates(
    candidates: list[TradeCandidate],
    *,
    source: DecisionSource = DecisionSource.SCAN,
) -> int:
    """Freeze all actionable candidates into decision snapshots. Returns new count."""
    snaps = [
        snap
        for c in candidates
        if (snap := snapshot_from_candidate(c, source=source)) is not None
    ]
    if not snaps:
        return 0
    added = await asyncio.to_thread(repository.save_snapshots, snaps)
    log.info("decisions_warehoused", candidates=len(candidates), new_snapshots=added)
    return added


def _eligible(snapshot, today, min_age_days: int, at_expiry_only: bool) -> bool:
    elapsed = (today - snapshot.generated_at.date()).days
    if elapsed < min_age_days:
        return False
    if at_expiry_only:
        return snapshot.expiration is not None and today >= snapshot.expiration
    return True


async def resolve_pending(
    *,
    min_age_days: int = 1,
    at_expiry_only: bool = False,
    limit: int = 500,
) -> list[DecisionOutcome]:
    """Resolve eligible pending decisions against current underlying prices."""
    now = datetime.now(UTC)
    today = now.date()
    pending = await asyncio.to_thread(repository.list_snapshots, limit, "pending")
    eligible = [
        s for s in pending if _eligible(s, today, min_age_days, at_expiry_only)
    ]
    if not eligible:
        return []

    market = registry.market_data_provider()
    # One quote per distinct symbol.
    spot: dict[str, float] = {}
    for sym in {s.symbol for s in eligible}:
        try:
            q = await market.get_quote(sym)
            spot[sym] = q.price
        except Exception as exc:  # a bad symbol must not stop the batch
            log.warning("resolve_quote_failed", symbol=sym, error=str(exc))

    resolved: list[DecisionOutcome] = []
    for snap in eligible:
        px = spot.get(snap.symbol)
        if px is None:
            continue
        outcome = resolve_underlying(snap, spot_now=px, resolved_at=now)
        await asyncio.to_thread(repository.save_outcome, outcome)
        resolved.append(outcome)

    log.info("decisions_resolved", eligible=len(eligible), resolved=len(resolved))
    return resolved
