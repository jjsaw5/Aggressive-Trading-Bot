"""Re-plan open positions onto DTE-aware exit discipline.

Positions opened before the decay-regime change carry a flat 50% target / 50%
stop / 7-DTE time stop regardless of expiry — which nags on day one of a 1-DTE
trade and lets a 40-DTE thesis rot. This recomputes the exit parameters from the
DTE each position was OPENED at (see position_import.dte_regime) and rebuilds
its exit plan.

Never touches: entry fills, P&L, status, or a recorded invalidation level.

Usage:
    python scripts/replan_open_positions.py            # dry-run report
    python scripts/replan_open_positions.py --apply    # write
"""

from __future__ import annotations

import argparse

from app.db import repository
from app.domain.enums import PaperTradeStatus
from app.risk.exit_plan import for_trade_plan
from app.services.position_import import dte_regime


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()

    rows = [t for t in repository.list_paper_trades(2000)
            if t.status == PaperTradeStatus.OPEN and t.trade_plan]
    print(f"--- replan {'APPLIED' if args.apply else 'DRY-RUN'} · {len(rows)} open position(s) ---")
    changed = 0
    for t in sorted(rows, key=lambda x: x.symbol):
        plan = t.trade_plan
        entry_dte = max(0, (min(lg.expiration for lg in plan.legs) - t.opened_at.date()).days)
        regime = dte_regime(entry_dte)
        risk = plan.risk
        before = (risk.profit_target_pct, risk.stop_loss_pct, risk.time_stop_dte, risk.dte_regime)
        after = (regime.profit_target_pct, regime.stop_loss_pct, regime.time_stop_dte, regime.name)
        if before == after:
            print(f"  {t.symbol:6} entry_dte={entry_dte:<3} already {regime.name} — unchanged")
            continue
        changed += 1
        print(f"  {t.symbol:6} entry_dte={entry_dte:<3} {before[3] or 'flat'} -> {regime.name}: "
              f"target {before[0]:.0%}->{after[0]:.0%}, time_stop {before[2]}->{after[2]} DTE")
        if not args.apply:
            continue
        risk.profit_target_pct = regime.profit_target_pct
        risk.stop_loss_pct = regime.stop_loss_pct
        risk.time_stop_dte = regime.time_stop_dte
        risk.dte_regime = regime.name
        # Keep an explicitly recorded thesis; only replace the placeholder note.
        if risk.invalidation_price is None:
            risk.invalidation_note = regime.note
        plan.exit_plan = for_trade_plan(plan)
        repository.save_paper_trade(t)
    print(f"\n{changed} position(s) {'re-planned' if args.apply else 'would change'}.")


if __name__ == "__main__":
    main()
