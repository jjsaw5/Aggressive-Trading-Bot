"""Grade pending decisions at their own expiry, so the app has a track record.

The warehouse accumulated thousands of recorded engine decisions and none were
ever graded, so the conviction gate reported n_decisive=0 — the app has been
predicting constantly and never checking itself.

This settles every pending decision whose expiry has PASSED, using the
underlying's close on that expiry date (exact intrinsic; see
app.analytics.expiry_settlement). Decisions still alive are left pending.

Cost discipline: one daily-price-history call per SYMBOL, not per decision —
thousands of snapshots resolve from a few dozen fetches.

Usage:
    python scripts/settle_pending_decisions.py                  # dry-run report
    python scripts/settle_pending_decisions.py --apply          # write outcomes
    python scripts/settle_pending_decisions.py --apply --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import UTC, date, datetime

from app.analytics.expiry_settlement import plan_expiry, settle_at_expiry
from app.db import repository
from app.logging_config import get_logger
from app.providers import registry

log = get_logger(__name__)


def _closes_by_date(history) -> dict[date, float]:
    return {c.ts.date(): c.close for c in history.candles}


def _close_on_or_before(closes: dict[date, float], target: date) -> float | None:
    """The settlement close. Expiry is normally a trading day, but a holiday or
    a data gap means walking back a few sessions — never forward, which would
    use information the decision could not have had."""
    for back in range(0, 7):
        px = closes.get(date.fromordinal(target.toordinal() - back))
        if px is not None:
            return px
    return None


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write outcomes (default: dry-run)")
    ap.add_argument("--limit", type=int, default=20000, help="max pending snapshots to load")
    args = ap.parse_args()

    now = datetime.now(UTC)
    today = now.date()
    pending = await asyncio.to_thread(repository.list_snapshots, args.limit, "pending")
    print(f"--- settle-at-expiry {'APPLIED' if args.apply else 'DRY-RUN'} ---")
    print(f"pending snapshots loaded: {len(pending)}")

    # Only decisions whose horizon has actually arrived can be graded.
    ripe, unripe, malformed = [], 0, 0
    for s in pending:
        if s.trade_plan is None or not s.trade_plan.legs:
            malformed += 1
            continue
        exp = plan_expiry(s.trade_plan)
        if exp is None:
            malformed += 1
        elif exp >= today:
            unripe += 1
        else:
            ripe.append((s, exp))
    print(f"  still live (expiry >= today): {unripe}")
    print(f"  no usable structure:          {malformed}")
    print(f"  ready to settle:              {len(ripe)}")
    if not ripe:
        return

    by_symbol: dict[str, list] = defaultdict(list)
    for s, exp in ripe:
        by_symbol[s.symbol].append((s, exp))
    oldest = min(exp for _s, exp in ripe)
    lookback = (today - oldest).days + 30
    print(f"  symbols: {len(by_symbol)} · daily history lookback: {lookback}d")

    market = registry.market_data_provider()
    outcomes, no_close, failed_symbols = [], 0, []
    for i, (symbol, items) in enumerate(sorted(by_symbol.items()), start=1):
        try:
            history = await market.get_price_history(symbol, lookback_days=lookback)
        except Exception as exc:  # noqa: BLE001 — one bad symbol must not stop the batch
            failed_symbols.append(symbol)
            log.warning("settle_history_failed", symbol=symbol, error=str(exc))
            continue
        closes = _closes_by_date(history)
        for s, exp in items:
            px = _close_on_or_before(closes, exp)
            if px is None:
                no_close += 1
                continue
            o = settle_at_expiry(s, px, resolved_at=now)
            if o is not None:
                outcomes.append(o)
        if i % 10 == 0:
            print(f"    …{i}/{len(by_symbol)} symbols, {len(outcomes)} settled")

    print(f"\nsettled: {len(outcomes)} · no close for expiry: {no_close} · "
          f"symbol fetch failures: {len(failed_symbols)}")
    if failed_symbols:
        print("  failed symbols:", ", ".join(sorted(failed_symbols)[:15]))

    tally = Counter(o.result.value for o in outcomes)
    total = sum(o.realized_pnl_usd or 0.0 for o in outcomes)
    decisive = tally["win"] + tally["loss"]
    print(f"results: {dict(tally)}")
    if decisive:
        print(f"win rate (decisive): {tally['win'] / decisive:.1%}")
    print(f"net P&L across settled decisions: ${total:,.2f}  "
          f"(hold-to-expiry policy, NOT the managed exit plan)")
    dirs = [o.direction_correct for o in outcomes if o.direction_correct is not None]
    if dirs:
        print(f"direction correct: {sum(dirs)}/{len(dirs)} = {sum(dirs) / len(dirs):.1%}")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply.")
        return

    for n, o in enumerate(outcomes, start=1):
        await asyncio.to_thread(repository.save_outcome, o)
        if n % 250 == 0:
            print(f"    wrote {n}/{len(outcomes)}")
    print(f"\nwrote {len(outcomes)} outcome(s).")


if __name__ == "__main__":
    asyncio.run(main())
