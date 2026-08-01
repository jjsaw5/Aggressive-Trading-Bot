"""Re-grade decisions at MINUTE resolution, with the cost stress attached.

Phase 2 of the remediation directive. The daily replay
(`scripts/settle_under_policy.py`) cannot see a same-session exit, which is why
every audited 0DTE signal resolved `expiry`. This runs the same policy against
1-minute bars, so the exit that actually happened is the exit that gets recorded
— with its price, its timestamp, its excursion, and what it would have made on
worse fills.

Grades are written under `outcome_source="managed_policy_intraday"` and never
overwrite the daily `managed_policy` rows. Both are kept: the pair is itself
evidence about how much the daily approximation was distorting the record.

    python scripts/settle_intraday.py --limit 20            # dry run
    python scripts/settle_intraday.py --limit 20 --apply
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import date, timedelta

from app.analytics.cost_stress import (
    SOURCE_EFFECTIVE,
    effective_half_spread,
    stress_pnl,
)
from app.analytics.intraday_settlement import load_minute_bars, settle_intraday
from app.db import repository
from app.logging_config import get_logger

log = get_logger(__name__)


def _sessions(start: date, end: date, cap: int = 10) -> list[date]:
    """Weekday sessions in [start, end], capped.

    The cap is a rate-limit guard, not a modelling choice: one call per leg per
    session means a long hold on a wide structure can be dozens of requests.
    Holidays are not filtered — a market-closed day simply returns no bars.
    """
    out, d = [], start
    while d <= end and len(out) < cap:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=50, help="max decisions to regrade")
    ap.add_argument("--apply", action="store_true", help="persist (default: dry run)")
    ap.add_argument("--max-sessions", type=int, default=10,
                    help="cap sessions fetched per decision (rate-limit guard)")
    args = ap.parse_args()

    snaps = repository.list_snapshots(limit=args.limit * 4)
    # Only decisions whose window has closed; an open trade has no final grade.
    today = date.today()
    todo = [
        s for s in snaps
        if s.trade_plan and s.expiration and s.expiration <= today
    ][: args.limit]
    print(f"decisions to regrade at minute resolution: {len(todo)}")
    if not todo:
        return 0

    reasons: Counter[str] = Counter()
    graded = abstained = 0

    for i, s in enumerate(todo, start=1):
        sessions = _sessions(
            s.generated_at.date(), min(s.expiration, today), cap=args.max_sessions
        )
        bars = await load_minute_bars(s.trade_plan, sessions)
        if not bars:
            abstained += 1
            continue

        # 0DTE is flattened at the close by policy, never held to expiry.
        same_day = s.expiration == s.generated_at.date()
        outcome = settle_intraday(s, bars, session_close_exit=same_day)
        if outcome is None:
            abstained += 1
            continue

        # Cost stress from the SAME bars that produced the grade, so the spread
        # used is the one that actually prevailed while the position was open.
        flat = [b for minute in bars.values() for b in minute.values()]
        half = effective_half_spread(flat)
        st = stress_pnl(
            s.trade_plan, outcome.realized_pnl_usd or 0.0, s.contracts,
            half_spread_per_leg=half, source=SOURCE_EFFECTIVE,
        )
        outcome.pnl_at_1tick_worse_usd = st.pnl_1tick_usd
        outcome.pnl_at_half_spread_worse_usd = st.pnl_half_spread_usd
        outcome.cost_stress_source = st.source

        reasons[outcome.exit_reason] += 1
        graded += 1
        if args.apply:
            repository.save_outcome(outcome)
        if i % 10 == 0:
            print(f"  …{i}/{len(todo)} processed, {graded} graded, {abstained} abstained")

    print(f"\ngraded: {graded}   abstained (no bars / still open): {abstained}")
    print(f"exit reasons: {dict(reasons)}")
    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to persist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
