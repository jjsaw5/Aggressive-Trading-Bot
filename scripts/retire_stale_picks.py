"""Clear engine-pick flags left behind by scans that never retired them.

The pick flag was write-only: `mark_engine_picks` set it on each scan's
candidates and nothing ever cleared the previous scan's. Flags accumulated
across sessions, so a board that commits to three setups was serving ten — and
because the backlog was dominated by an older, bearish session, a strongly
bullish tape still rendered as uniformly bearish.

Detection now retires the prior scan's picks before marking new ones, so this is
only needed once, to clear what accumulated before that landed. Running it again
is harmless: it retires everything older than the newest scan per board, which is
exactly the invariant.

Usage:
    python scripts/retire_stale_picks.py           # dry-run report
    python scripts/retire_stale_picks.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from app.db import repository
from app.shortduration.ranking import retire_engine_picks

_BOARDS = ("0dte", "1-5dte")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--keep-newest-scan", action="store_true", default=True,
                    help="keep picks from each board's most recent scan (default)")
    args = ap.parse_args()

    now = datetime.now(UTC)
    print(f"--- retire-stale-picks {'APPLIED' if args.apply else 'DRY-RUN'} ---")
    total = 0
    for board in _BOARDS:
        rows = await asyncio.to_thread(
            repository.list_short_duration_candidates,
            dte_category=board, limit=1000, ranked=False,
        )
        flagged = [c for c in rows if c.engine_pick]
        if not flagged:
            print(f"{board}: nothing flagged")
            continue

        # The newest scan owns the live pick list. Candidates from one scan share
        # a detection timestamp to the second, so that instant identifies it.
        newest = max(c.detected_at for c in flagged)
        keep = {c.id for c in flagged if args.keep_newest_scan
                and (newest - c.detected_at).total_seconds() < 60}

        print(f"\n{board}: {len(flagged)} flagged · keeping {len(keep)} from the "
              f"newest scan ({newest:%Y-%m-%d %H:%M} UTC)")
        for c in sorted(flagged, key=lambda x: x.detected_at, reverse=True):
            age_m = (now - c.detected_at).total_seconds() / 60
            verdict = "KEEP " if c.id in keep else "retire"
            print(f"  {verdict} {c.symbol:6s} {c.direction.value:8s} "
                  f"rank={c.pick_rank} state={c.state.value:10s} age={age_m:6.0f}m")

        retired = retire_engine_picks(flagged, keep_ids=keep)
        total += len(retired)
        if args.apply:
            for cand in retired:
                await asyncio.to_thread(repository.save_short_duration_candidate, cand)

    print(f"\n{'retired' if args.apply else 'would retire'}: {total} pick flag(s)")
    if not args.apply:
        print("Dry run — nothing written. Re-run with --apply.")


if __name__ == "__main__":
    asyncio.run(main())
