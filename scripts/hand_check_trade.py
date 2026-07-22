"""Dump every number behind ONE real-mark trade so it can be checked by hand.

Prints the entry/exit bars (raw NBBO), each leg's fill at k=0.5 and k=1.0, the
net debit/credit, gross and net P&L — then re-derives the same figures through
the production evaluator and asserts they agree. If the plumbing is right, the
hand arithmetic and the evaluator match to the cent.

    UW_HISTORIC_ENABLED=true UNUSUAL_WHALES_API_KEY=... \
        python -m scripts.hand_check_trade
"""

from __future__ import annotations

import asyncio
from datetime import date

from app.backtest.fill_model import leg_fill, round_trip_commission, round_trip_pnl
from app.backtest.real_mark import ExitRule, RealMarkTrade, evaluate_real_mark_trade
from app.providers import registry

# A concrete SPY Jun-2022 bull-call-debit spread: long 400C, short 420C.
LONG = "SPY220617C00400000"
SHORT = "SPY220617C00420000"
EXPIRY = date(2022, 6, 17)
ENTRY_TD_BEFORE = 40  # entry ~40 trading days before expiry
CONTRACTS = 1
K_OPT, K_CON = 0.5, 1.0


def _fmt(b) -> str:
    return (f"{b.date}  bid={b.nbbo_bid:>6}  ask={b.nbbo_ask:>6}  "
            f"mid={b.mid:>7.4f}  half={b.half_spread:>6.4f}  oi={b.open_interest}  vol={b.volume}")


async def main() -> None:
    prov = registry.historical_options_provider()
    lbars = await prov.get_contract_history(LONG)
    sbars = await prov.get_contract_history(SHORT)
    await prov.aclose()

    # Entry = an actual aligned trading day, ~ENTRY_TD_BEFORE days before expiry.
    aligned = sorted({b.date for b in lbars} & {b.date for b in sbars})
    entry_date = aligned[-(ENTRY_TD_BEFORE + 1)]

    rule = ExitRule(profit_target_pct=0.5, stop_loss_pct=0.5, time_stop_days=21)
    res = evaluate_real_mark_trade(
        RealMarkTrade("hand", LONG, SHORT, entry_date, dte_at_entry=(EXPIRY - entry_date).days,
                      contracts=CONTRACTS, strategy="call_debit_spread", direction="bullish"),
        lbars, sbars, rule,
    )
    if not res.included:
        print(f"trade excluded: {res.exclusion_reason} (entry {entry_date})")
        return
    el = next(b for b in lbars if b.date == res.entry_date)
    es = next(b for b in sbars if b.date == res.entry_date)
    xl = next(b for b in lbars if b.date == res.exit_date)
    xs = next(b for b in sbars if b.date == res.exit_date)

    print(f"TRADE  long {LONG}  /  short {SHORT}  x{CONTRACTS}")
    print(f"       entry {res.entry_date} -> exit {res.exit_date} "
          f"({res.days_held} trading days, {res.exit_reason})\n")
    print("ENTRY BARS")
    print("  long ", _fmt(el))
    print("  short", _fmt(es))
    print("EXIT BARS")
    print("  long ", _fmt(xl))
    print("  short", _fmt(xs))

    for k in (K_OPT, K_CON):
        print(f"\n--- k = {k} (0.5 = fill at mid-ish, 1.0 = pay the spread) ---")
        buy_open = leg_fill(el, "buy", k)
        sell_open = leg_fill(es, "sell", k)
        entry_net = buy_open - sell_open
        print(f"  OPEN : buy long  = mid {el.mid:.4f} + {k}*half {el.half_spread:.4f} = {buy_open:.4f}")
        print(f"         sell short = mid {es.mid:.4f} - {k}*half {es.half_spread:.4f} = {sell_open:.4f}")
        print(f"         entry net debit = {buy_open:.4f} - {sell_open:.4f} = {entry_net:.4f}/sh")
        sell_close = leg_fill(xl, "sell", k)
        buy_close = leg_fill(xs, "buy", k)
        exit_net = sell_close - buy_close
        print(f"  CLOSE: sell long = mid {xl.mid:.4f} - {k}*half {xl.half_spread:.4f} = {sell_close:.4f}")
        print(f"         buy short = mid {xs.mid:.4f} + {k}*half {xs.half_spread:.4f} = {buy_close:.4f}")
        print(f"         exit net  = {sell_close:.4f} - {buy_close:.4f} = {exit_net:.4f}/sh")
        gross = (exit_net - entry_net) * 100 * CONTRACTS
        comm = round_trip_commission(contracts=CONTRACTS)
        net = gross - comm
        print(f"  P&L  : gross = ({exit_net:.4f} - {entry_net:.4f}) * 100 * {CONTRACTS} = {gross:+.2f}")
        print(f"         commissions (4 legs @ $0.65) = {comm:.2f}")
        print(f"         NET = {gross:+.2f} - {comm:.2f} = {net:+.2f}")
        # Re-derive via the production path and assert equality.
        rt = round_trip_pnl(el, es, xl, xs, k=k, contracts=CONTRACTS)
        ev = res.by_k[k]
        assert abs(rt.net_pnl_usd - net) < 1e-6, (rt.net_pnl_usd, net)
        assert abs(ev.net_pnl_usd - net) < 1e-6, (ev.net_pnl_usd, net)
        print(f"  CHECK: evaluator net = {ev.net_pnl_usd:+.2f}  (matches hand arithmetic ✓)")


if __name__ == "__main__":
    asyncio.run(main())
