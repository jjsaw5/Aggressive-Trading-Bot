"""Sync Robinhood option history into tracked positions (source: rh_sync).

Input is a raw dump of the Robinhood MCP ``get_option_orders`` response (all
FILLED orders). The script rebuilds round-trip *episodes* per symbol from the
individual leg executions, then reconciles them with the tracked-positions
table:

- an episode already tracked (e.g. entered manually via quick-add) is left
  alone — except that a tracked OPEN position whose contracts are flat at the
  broker is closed in place with the real exit fill;
- a closed episode nobody tracked becomes a CLOSED rh_sync trade with a
  ``live_close`` outcome (real realized P&L, fidelity 3);
- a still-open episode nobody tracked becomes an OPEN rh_sync trade.

Episode model: contracts are tracked per (type, strike, expiration) in a
per-symbol ledger. An episode runs from the moment a symbol's exposure leaves
zero until every contract is flat again — this correctly absorbs spreads that
were opened as one order but closed leg by leg. Contracts still open past
their expiration are synthesized shut at $0.00 on expiration day (reason:
expiry). Sign convention throughout: net per share is debit > 0 / credit < 0
at entry; at exit, credit received > 0 / debit paid < 0 — so realized P&L is
always ``(exit - entry) * 100 * contracts``.

Idempotent: episode ids are deterministic (``rh`` + hash of symbol, legs and
open date), so re-runs — including the scheduled open/close sync — create
nothing new. Unrecognized structures (e.g. rolls merged into one episode) are
reported and skipped, never guessed at.

Usage:
    python scripts/rh_sync.py --orders rh_orders.json          # dry-run report
    python scripts/rh_sync.py --orders rh_orders.json --apply  # write to DB
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time

from app.domain.enums import ExitReason, OptionType, PaperTradeStatus
from app.domain.trades import PaperTrade
from app.services.position_import import ImportedLeg, build_tracked_trade

_EXPIRY_CLOSE_UTC = time(20, 0)  # 4:00 pm ET (EDT) settlement stamp


@dataclass
class Fill:
    ts: datetime
    symbol: str
    option_type: str  # "call" | "put"
    strike: float
    expiration: date
    signed_qty: float  # + buy, - sell
    price: float  # per share
    effect: str  # "open" | "close" | "expiry"
    order_id: str = ""

    @property
    def contract(self) -> tuple:
        return (self.option_type, self.strike, self.expiration)


@dataclass
class Episode:
    symbol: str
    fills: list[Fill] = field(default_factory=list)

    @property
    def opened_at(self) -> datetime:
        return min(f.ts for f in self.fills if f.effect == "open")

    @property
    def closed_at(self) -> datetime | None:
        closing = [f for f in self.fills if f.effect != "open"]
        if not closing:
            return None
        return max(f.ts for f in closing)

    def leg_key(self) -> tuple:
        legs = self.opening_legs()
        return tuple(sorted((t, s, e.isoformat()) for (t, s, e), _q, _p in legs))

    def opening_legs(self) -> list[tuple[tuple, float, float]]:
        """[(contract, net_opened_qty, avg_entry_price_per_share)] — signed qty."""
        qty: dict[tuple, float] = defaultdict(float)
        cost: dict[tuple, float] = defaultdict(float)
        for f in self.fills:
            if f.effect != "open":
                continue
            k = (f.option_type, f.strike, f.expiration)
            qty[k] += f.signed_qty
            cost[k] += f.price * abs(f.signed_qty)
        out = []
        for k, q in qty.items():
            opened = sum(abs(f.signed_qty) for f in self.fills
                         if f.effect == "open" and (f.option_type, f.strike, f.expiration) == k)
            avg = cost[k] / opened if opened else 0.0
            if q != 0:
                out.append((k, q, round(avg, 4)))
        return sorted(out, key=lambda x: (x[0][0], x[0][1]))

    def contracts(self) -> int:
        legs = self.opening_legs()
        return int(min(abs(q) for _k, q, _p in legs)) if legs else 0

    def entry_net(self) -> float:
        n = self.contracts() or 1
        total = sum(f.price * f.signed_qty for f in self.fills if f.effect == "open")
        return round(total / n, 4)

    def exit_net(self) -> float | None:
        closing = [f for f in self.fills if f.effect != "open"]
        if not closing:
            return None
        n = self.contracts() or 1
        # Selling to close receives (+), buying to close pays (−).
        total = sum(f.price * -f.signed_qty for f in closing)
        return round(total / n, 4)

    def is_closed(self) -> bool:
        qty: dict[tuple, float] = defaultdict(float)
        for f in self.fills:
            qty[(f.option_type, f.strike, f.expiration)] += f.signed_qty
        return all(abs(q) < 1e-9 for q in qty.values())

    def exit_reason(self) -> ExitReason:
        closing = [f for f in self.fills if f.effect != "open"]
        last = max(closing, key=lambda f: f.ts)
        return ExitReason.EXPIRY if last.effect == "expiry" else ExitReason.MANUAL

    def trade_id(self) -> str:
        raw = f"{self.symbol}|{self.leg_key()}|{self.opened_at.date().isoformat()}"
        return "rh" + hashlib.sha1(raw.encode()).hexdigest()[:10]


def load_fills(orders_payload: dict) -> list[Fill]:
    fills: list[Fill] = []
    for o in orders_payload["data"]["orders"]:
        if o.get("state") != "filled":
            continue
        sym = o["chain_symbol"].upper()
        for leg in o.get("legs", []):
            sign = 1 if leg["side"] == "buy" else -1
            for ex in leg.get("executions", []):
                fills.append(Fill(
                    ts=datetime.fromisoformat(ex["timestamp"].replace("Z", "+00:00")),
                    symbol=sym,
                    option_type=leg["option_type"],
                    strike=float(leg["strike_price"]),
                    expiration=date.fromisoformat(leg["expiration_date"]),
                    signed_qty=sign * float(ex["quantity"]),
                    price=float(ex["price"]),
                    effect=leg["position_effect"],
                    order_id=str(o.get("id", "")),
                ))
    fills.sort(key=lambda f: f.ts)
    return fills


def _split_flat(sym: str, group: list[Fill], as_of: date) -> list[Episode]:
    """Chronological fills for one linked contract group -> episodes, splitting
    whenever the group's exposure returns to zero, and synthesizing $0 expiry
    fills for contracts held past expiration."""
    episodes: list[Episode] = []
    ledger: dict[tuple, float] = defaultdict(float)
    current: Episode | None = None

    def expire_stale(up_to: date, cur: Episode | None) -> Episode | None:
        for k, q in list(ledger.items()):
            typ, strike, exp = k
            if abs(q) > 1e-9 and exp < up_to:
                stamp = datetime.combine(exp, _EXPIRY_CLOSE_UTC, tzinfo=UTC)
                if cur is not None:
                    cur.fills.append(Fill(
                        ts=stamp, symbol=sym, option_type=typ, strike=strike,
                        expiration=exp, signed_qty=-q, price=0.0, effect="expiry",
                    ))
                ledger[k] = 0.0
        if cur is not None and cur.is_closed() and any(
                f.effect != "open" for f in cur.fills):
            episodes.append(cur)
            return None
        return cur

    for f in sorted(group, key=lambda x: x.ts):
        current = expire_stale(f.ts.date(), current)
        if current is None:
            current = Episode(symbol=sym)
        current.fills.append(f)
        ledger[f.contract] += f.signed_qty
        if current.is_closed():
            episodes.append(current)
            current = None
    current = expire_stale(as_of, current)
    if current is not None and current.fills:
        episodes.append(current)  # still open
    return episodes


def build_episodes(fills: list[Fill], *, as_of: date | None = None) -> list[Episode]:
    """Group fills into round-trip episodes. Contracts are linked when they ever
    appear in the same order (a spread's legs), so independent trades that merely
    overlap in time on the same symbol stay separate; each linked group is then
    split at the points where its exposure returns to zero."""
    as_of = as_of or datetime.now(UTC).date()

    # Union-find over (symbol, contract), linked by order co-occurrence.
    parent: dict[tuple, tuple] = {}

    def find(x: tuple) -> tuple:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: tuple, b: tuple) -> None:
        parent[find(a)] = find(b)

    by_order: dict[tuple, list[Fill]] = defaultdict(list)
    for f in fills:
        by_order[(f.symbol, f.order_id)].append(f)
    for (sym, _oid), group in by_order.items():
        first = (sym, group[0].contract)
        for f in group[1:]:
            union((sym, f.contract), first)

    groups: dict[tuple, list[Fill]] = defaultdict(list)
    for f in fills:
        groups[find((f.symbol, f.contract))].append(f)

    episodes: list[Episode] = []
    for (sym, _root), group in groups.items():
        episodes.extend(_split_flat(sym, group, as_of))
    return sorted(episodes, key=lambda e: e.opened_at)


def split_per_contract(ep: Episode, *, as_of: date | None = None) -> list[Episode]:
    """Fallback for an unrecognized multi-leg episode: treat each contract as its
    own single-leg trade (used for e.g. two independent long calls bought in one
    order — there are no spread economics to preserve)."""
    as_of = as_of or datetime.now(UTC).date()
    by_contract: dict[tuple, list[Fill]] = defaultdict(list)
    for f in ep.fills:
        by_contract[f.contract].append(f)
    out: list[Episode] = []
    for group in by_contract.values():
        out.extend(_split_flat(ep.symbol, [f for f in group if f.effect != "expiry"], as_of))
    return out


def _matches_tracked(ep: Episode, t: PaperTrade) -> bool:
    """Same symbol, same contract set, opened within a few days — the episode is
    the broker-side view of an already-tracked position (usually manual entry)."""
    if t.symbol.upper() != ep.symbol or not t.trade_plan:
        return False
    tracked = tuple(sorted(
        (lg.option_type.value, lg.strike, lg.expiration.isoformat())
        for lg in t.trade_plan.legs))
    if tracked != ep.leg_key():
        return False
    return abs((t.opened_at.date() - ep.opened_at.date()).days) <= 5


def episode_to_trade(ep: Episode) -> PaperTrade:
    legs = [ImportedLeg(
        strike=strike, option_type=OptionType(typ), is_long=q > 0,
        quantity=int(abs(q)), entry_price_per_share=price, expiration=exp,
    ) for (typ, strike, exp), q, price in ep.opening_legs()]
    if len(legs) == 1 and not legs[0].is_long:
        raise ValueError("standalone short leg — not a defined-risk structure")
    t = build_tracked_trade(
        ep.symbol, legs, opened_at=ep.opened_at, source="rh_sync",
        net_per_share=ep.entry_net(),
    )
    t.id = ep.trade_id()
    return t


def sync(orders_payload: dict, *, apply: bool = False) -> dict:
    from app.db import repository

    fills = load_fills(orders_payload)
    eps = build_episodes(fills)
    existing = repository.list_paper_trades(2000)
    by_id = {t.id: t for t in existing}

    report = {"created_closed": [], "created_open": [], "closed_in_place": [],
              "already_tracked": [], "split": [], "skipped": []}

    work = list(eps)
    while work:
        ep = work.pop(0)
        label = (f"{ep.symbol} {ep.leg_key()} x{ep.contracts()} "
                 f"open {ep.opened_at.date()} entry {ep.entry_net():+.2f}"
                 + (f" -> exit {ep.exit_net():+.2f} ({ep.exit_reason().value})"
                    f" closed {ep.closed_at.date()}" if ep.is_closed() else " [OPEN]"))
        if ep.trade_id() in by_id:
            tracked = by_id[ep.trade_id()]
        else:
            tracked = next((t for t in existing if _matches_tracked(ep, t)), None)

        if tracked is not None:
            if ep.is_closed() and tracked.status == PaperTradeStatus.OPEN:
                # Broker says flat: close the tracked position with real fills.
                if apply:
                    _close_in_place(repository, tracked, ep)
                report["closed_in_place"].append(f"{tracked.id} {label}")
            else:
                report["already_tracked"].append(f"{tracked.id} {label}")
            continue

        try:
            t = episode_to_trade(ep)
        except ValueError as exc:
            subs = split_per_contract(ep)
            if len(subs) > 1:
                # e.g. two independent long calls bought as one order: track each
                # contract as its own trade rather than dropping the history.
                report["split"].append(f"{label} -> {len(subs)} single-leg trades")
                work = subs + work
            else:
                report["skipped"].append(f"{label} — {exc}")
            continue

        if ep.is_closed():
            t.status = PaperTradeStatus.CLOSED
            t.closed_at = ep.closed_at
            t.exit_fill = ep.exit_net()
            t.exit_reason = ep.exit_reason()
            t.exit_note = "backfilled from Robinhood history"
            t.realized_pnl_usd = round(
                (t.exit_fill - t.entry_fill) * 100 * t.trade_plan.contracts, 2)
            if apply:
                repository.save_paper_trade(t)
                _record_outcome(repository, t)
            report["created_closed"].append(f"{t.id} {label} pnl {t.realized_pnl_usd:+.2f}")
        else:
            if apply:
                repository.save_paper_trade(t)
            report["created_open"].append(f"{t.id} {label}")
    return report


def _close_in_place(repository, t: PaperTrade, ep: Episode) -> None:
    t.status = PaperTradeStatus.CLOSED
    t.closed_at = ep.closed_at
    t.exit_fill = ep.exit_net()
    t.exit_reason = ep.exit_reason()
    t.exit_note = (t.exit_note or "") or "closed via Robinhood sync"
    t.realized_pnl_usd = round(
        (t.exit_fill - t.entry_fill) * 100 * t.trade_plan.contracts, 2)
    repository.save_paper_trade(t)
    _record_outcome(repository, t)


def _record_outcome(repository, t: PaperTrade) -> None:
    """Same grading as the dashboard close flow (live_close, fidelity 3)."""
    from app.api.routes.positions import _record_live_close

    _record_live_close(t, t.closed_at)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--orders", required=True, help="raw get_option_orders JSON dump")
    ap.add_argument("--apply", action="store_true", help="write to the DB (default: dry-run)")
    args = ap.parse_args()

    with open(args.orders) as fh:
        payload = json.load(fh)
    report = sync(payload, apply=args.apply)
    print(f"--- rh_sync {'APPLIED' if args.apply else 'DRY-RUN'} ---")
    for section, rows in report.items():
        print(f"\n{section} ({len(rows)}):")
        for r in rows:
            print("  " + r)


if __name__ == "__main__":
    main()
