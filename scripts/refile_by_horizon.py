"""Refile existing candidates onto the board matching their contract's horizon.

The 1-5DTE board was 65% swing-horizon contracts (21-45 DTE), because a
daily-trend thesis is deliberately expressed weeks out (contracts.is_swing) and
the candidate was filed under the SCAN that produced it rather than the horizon
it can actually be traded at. "1-5DTE" therefore did not mean 1-5 DTE.

Detection now routes new candidates by contract horizon. This moves the rows
written before that landed. 0DTE is never touched — it is same-day by definition
and its contracts already match.

Usage:
    python scripts/refile_by_horizon.py            # dry-run report
    python scripts/refile_by_horizon.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from app.db import repository
from app.domain.enums import DTECategory
from app.shortduration.ranking import board_for, contract_horizon_days


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--limit", type=int, default=5000, help="rows to scan per board")
    args = ap.parse_args()

    print(f"--- refile-by-horizon {'APPLIED' if args.apply else 'DRY-RUN'} ---")
    moves: Counter[str] = Counter()
    changed = []

    for board in (DTECategory.SHORT_DTE, DTECategory.MEDIUM_DTE):
        rows = await asyncio.to_thread(
            repository.list_short_duration_candidates,
            dte_category=board.value, limit=args.limit, ranked=False,
        )
        print(f"\n{board.value}: {len(rows)} row(s)")
        for c in rows:
            target = board_for(c)
            if target == c.dte_category:
                continue
            days = contract_horizon_days(c)
            moves[f"{c.dte_category.value} -> {target.value}"] += 1
            c.dte_category = target
            changed.append(c)
            if len(changed) <= 15:
                print(f"   {c.symbol:6s} {c.direction.value:8s} {days:>3}d horizon "
                      f"-> {target.value}")

    print(f"\nmoves: {dict(moves) or 'none'}")
    print(f"{'refiled' if args.apply else 'would refile'}: {len(changed)} candidate(s)")
    if not args.apply:
        print("Dry run — nothing written. Re-run with --apply.")
        return
    for n, c in enumerate(changed, start=1):
        await asyncio.to_thread(repository.save_short_duration_candidate, c)
        if n % 200 == 0:
            print(f"    wrote {n}/{len(changed)}")
    print(f"wrote {len(changed)}.")


if __name__ == "__main__":
    asyncio.run(main())
