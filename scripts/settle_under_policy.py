"""Re-grade expired decisions under the exit policy the app actually runs.

`settle_pending_decisions.py` grades hold-to-expiry — the right answer for
probability-of-profit, the wrong answer for "would this strategy have made
money", because the plan takes profit at 40-60%, stops at -50%, and time-stops
by DTE regime. This walks each decision's real daily option marks forward from
entry, applies its own recorded exit rules, and books the first trigger.

Both grades are kept. The scorecard uses the hold-to-expiry outcome for win rate
and Brier (POP is a hold-to-expiry claim) and the managed replay for the dollar
metrics (see calibration.select_pnl_outcomes).

Cost discipline: the historical options feed is billed per contract, so every
unique OCC symbol is fetched ONCE and shared across every decision that used it,
and underlying history is one call per symbol. Decisions whose legs the feed
cannot price are ABSTAINED, never modelled.

Usage:
    python scripts/settle_under_policy.py                 # dry-run report
    python scripts/settle_under_policy.py --apply
    python scripts/settle_under_policy.py --apply --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta

from app.analytics.expiry_settlement import plan_expiry
from app.analytics.policy_settlement import occ_symbol, settle_under_policy
from app.db import repository
from app.logging_config import get_logger
from app.providers import registry

log = get_logger(__name__)

MARK_LOOKBACK_PAD = 3  # days of slack around entry so the first session is covered


def _close_on_or_before(closes: dict[date, float], target: date) -> float | None:
    """Never walk forward: that would hand the decision information it lacked."""
    for back in range(0, 7):
        px = closes.get(date.fromordinal(target.toordinal() - back))
        if px is not None:
            return px
    return None


async def _fetch_marks(hist, symbols: dict[str, tuple[date, date]]) -> dict[str, dict[date, float]]:
    """One call per unique contract; a failure abstains the decisions using it."""
    out: dict[str, dict[date, float]] = {}
    failures = 0
    for i, (sym, (start, end)) in enumerate(sorted(symbols.items()), start=1):
        try:
            series = await hist.get_option_mark_series(sym, start, end)
        except Exception as exc:  # noqa: BLE001 — one dead contract must not stop the batch
            failures += 1
            log.warning("policy_marks_failed", option_symbol=sym, error=str(exc))
            continue
        if series:
            out[sym] = {p.ts.date(): p.mark for p in series}
        if i % 25 == 0:
            print(f"    …{i}/{len(symbols)} contracts, {len(out)} priced, {failures} failed")
    print(f"  contracts priced: {len(out)}/{len(symbols)} (fetch failures: {failures})")
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write outcomes (default: dry-run)")
    ap.add_argument("--limit", type=int, default=20000, help="max snapshots to load")
    ap.add_argument("--max-contracts", type=int, default=1500,
                    help="cap on unique contract fetches (cost guard)")
    args = ap.parse_args()

    now = datetime.now(UTC)
    today = now.date()
    # Both statuses: 'resolved' snapshots already carry a hold-to-expiry grade and
    # are exactly the ones that need the managed grade alongside it.
    snaps = await asyncio.to_thread(repository.list_snapshots, args.limit, None)
    print(f"--- settle-under-policy {'APPLIED' if args.apply else 'DRY-RUN'} ---")
    print(f"snapshots loaded: {len(snaps)}")

    ripe: list[tuple[object, date]] = []
    skipped_live = skipped_malformed = skipped_done = 0
    for s in snaps:
        if s.trade_plan is None or not s.trade_plan.legs:
            skipped_malformed += 1
            continue
        exp = s.expiration or plan_expiry(s.trade_plan)
        if exp is None:
            skipped_malformed += 1
        elif exp >= today:
            skipped_live += 1
        else:
            ripe.append((s, exp))
    print(f"  still live:          {skipped_live}")
    print(f"  no usable structure: {skipped_malformed}")
    print(f"  expired:             {len(ripe)}")
    if not ripe:
        return

    # Skip anything already graded under this policy so a re-run is idempotent.
    fresh = []
    for s, exp in ripe:
        existing = await asyncio.to_thread(repository.get_outcomes_for, s.decision_id)
        if any(o.outcome_source == "managed_policy" for o in existing):
            skipped_done += 1
            continue
        fresh.append((s, exp))
    print(f"  already policy-graded: {skipped_done}")
    print(f"  to grade:              {len(fresh)}")
    if not fresh:
        return

    # Contract windows: union across every decision that touches the contract.
    windows: dict[str, tuple[date, date]] = {}
    for s, exp in fresh:
        start = s.generated_at.date() - timedelta(days=MARK_LOOKBACK_PAD)
        for leg in s.trade_plan.legs:
            sym = occ_symbol(leg.symbol, leg.expiration, leg.option_type, leg.strike)
            lo, hi = windows.get(sym, (start, exp))
            windows[sym] = (min(lo, start), max(hi, exp))
    print(f"  unique contracts:      {len(windows)}")
    if len(windows) > args.max_contracts:
        print(f"\nSTOP: {len(windows)} contract fetches exceeds --max-contracts "
              f"{args.max_contracts}. Lower --limit or raise the cap deliberately.")
        return

    try:
        hist = registry.historical_options_provider()
    except Exception as exc:  # noqa: BLE001
        print(f"\nSTOP: no historical options provider ({exc}). Cannot grade a policy "
              "without real marks, and modelling the path would be a lie.")
        return

    marks_by_symbol = await _fetch_marks(hist, windows)

    # Underlying closes for the expiry fall-through: one history call per symbol.
    market = registry.market_data_provider()
    oldest = min(exp for _s, exp in fresh)
    lookback = (today - oldest).days + 30
    closes_by_symbol: dict[str, dict[date, float]] = {}
    for symbol in sorted({s.symbol for s, _ in fresh}):
        try:
            history = await market.get_price_history(symbol, lookback_days=lookback)
        except Exception as exc:  # noqa: BLE001
            log.warning("policy_underlying_history_failed", symbol=symbol, error=str(exc))
            continue
        closes_by_symbol[symbol] = {c.ts.date(): c.close for c in history.candles}

    outcomes, abstained = [], 0
    exits: Counter[str] = Counter()
    for s, exp in fresh:
        per_date: dict[date, dict[str, float]] = defaultdict(dict)
        for leg in s.trade_plan.legs:
            sym = occ_symbol(leg.symbol, leg.expiration, leg.option_type, leg.strike)
            for d, mark in marks_by_symbol.get(sym, {}).items():
                per_date[d][sym] = mark
        px = _close_on_or_before(closes_by_symbol.get(s.symbol, {}), exp)
        o = settle_under_policy(s, dict(per_date), resolved_at=now,
                                underlying_close_at_expiry=px)
        if o is None:
            abstained += 1
            continue
        outcomes.append(o)
        exits[o.exit_reason or "unknown"] += 1

    print(f"\ngraded: {len(outcomes)} · abstained (unpriceable): {abstained}")
    if not outcomes:
        print("Nothing gradeable — the feed could not price these structures.")
        return
    print(f"exit reasons: {dict(exits)}")
    tally = Counter(o.result.value for o in outcomes)
    decisive = tally["win"] + tally["loss"]
    print(f"results: {dict(tally)}")
    if decisive:
        print(f"win rate (decisive): {tally['win'] / decisive:.1%}")
    total = sum(o.realized_pnl_usd or 0.0 for o in outcomes)
    print(f"net P&L under the MANAGED plan: ${total:,.2f} "
          f"(avg ${total / len(outcomes):,.2f}/decision)")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply.")
        return

    for n, o in enumerate(outcomes, start=1):
        await asyncio.to_thread(repository.save_outcome, o)
        if n % 250 == 0:
            print(f"    wrote {n}/{len(outcomes)}")
    print(f"\nwrote {len(outcomes)} managed-policy outcome(s).")


if __name__ == "__main__":
    asyncio.run(main())
