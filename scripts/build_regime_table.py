"""Build or extend the daily market-regime table (pre-flight P6).

Fetches ^VIX and ^GSPC daily closes, reduces them to one row per session, and
persists. Existing sessions are LEFT ALONE — a regime row describes a closed
session and never legitimately changes, so a vendor revision is a decision for a
human rather than a silent rewrite of rows other analyses already grouped by.

Run once before the capture window opens, then daily (or on each export).

    python scripts/build_regime_table.py                 # dry run
    python scripts/build_regime_table.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from app.analytics.daily_regime import build_regime_series, fetch_regime_inputs
from app.db import repository


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lookback-days", type=int, default=400,
                    help="calendar days of history to fetch (needs >=50 sessions "
                         "for the SMA and >=21 for realized vol)")
    ap.add_argument("--apply", action="store_true", help="persist (default: dry run)")
    args = ap.parse_args()

    vix, spx = await fetch_regime_inputs(args.lookback_days)
    print(f"^VIX sessions: {len(vix)}   ^GSPC sessions: {len(spx)}")
    if not vix or not spx:
        print("ABORT: one or both series unavailable. A regime row is a JOINT "
              "observation — half of one is not a reading.")
        return 2

    rows = build_regime_series(vix, spx)
    complete = [r for r in rows if r.is_complete]
    print(f"rows built: {len(rows)}   fully classified: {len(complete)}")
    if not rows:
        print("ABORT: no overlapping sessions between the two series.")
        return 2

    print(f"span: {rows[0].session} -> {rows[-1].session}")
    print(f"class distribution: {dict(Counter(r.regime_class for r in rows))}")
    print("\nlast 10 sessions:")
    print(f"  {'date':12s} {'vix':>7s} {'vix_pct':>8s} {'spx_rv20':>9s} "
          f"{'vs_50sma':>9s}  class")
    for r in rows[-10:]:
        def _n(v, nd=4):
            return f"{v:.{nd}f}" if v is not None else "—"
        print(f"  {r.session.isoformat():12s} {_n(r.vix_close, 2):>7s} "
              f"{_n(r.vix_percentile_20d):>8s} {_n(r.spx_realized_vol_20d):>9s} "
              f"{_n(r.spx_vs_50d_sma):>9s}  {r.regime_class}")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to persist.")
        return 0

    added = await asyncio.to_thread(repository.save_daily_regimes, rows)
    print(f"\npersisted: {added} new session(s); {len(rows) - added} already present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
