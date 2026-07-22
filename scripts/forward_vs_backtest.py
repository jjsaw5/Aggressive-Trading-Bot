"""Reprice the forward ledger's resolved trades against real UW marks and surface
every disagreement — especially sign flips (ledger win, real-mark loss).

Reads the resolved trades from the System-A Turso ledger, rebuilds each vertical's
OCC legs, pulls both legs' recorded NBBO history, reprices entry->resolution net of
costs, and compares to the ledger's booked P&L.

    UW_HISTORIC_ENABLED=true UNUSUAL_WHALES_API_KEY=... \
    TURSO_DATABASE_URL=libsql://ai-trade-agen-... TURSO_AUTH_TOKEN=... \
        python -m scripts.forward_vs_backtest

A read-only Turso token is sufficient (and safer). No credential is written to disk.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from datetime import date, datetime

from app.backtest.cross_check import LedgerTrade, cross_check_trade, summarize
from app.providers import registry

_OUTCOMES_SQL = """
SELECT o.recommendation_id, o.net_pnl, o.outcome, o.resolved_at, o.days_held,
       r.symbol, r.strategy_type, COALESCE(st.created_at, r.created_at) AS entry_at,
       COALESCE(r.contracts, 1) AS contracts
FROM trade_outcomes o
JOIN trade_recommendations r ON o.recommendation_id = r.recommendation_id
LEFT JOIN shadow_trades st ON st.recommendation_id = o.recommendation_id
"""
_LEGS_SQL = """
SELECT recommendation_id, action, leg_role, option_type, strike, expiration_date, option_symbol
FROM recommendation_legs
"""


def _turso_query(url: str, token: str, sql: str) -> list[dict]:
    https = url.replace("libsql://", "https://").rstrip("/") + "/v2/pipeline"
    body = json.dumps({"requests": [
        {"type": "execute", "stmt": {"sql": sql}}, {"type": "close"},
    ]}).encode()
    req = urllib.request.Request(https, data=body, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted host)
        payload = json.load(resp)
    res = payload["results"][0]["response"]["result"]
    cols = [c["name"] for c in res["cols"]]
    out = []
    for row in res["rows"]:
        out.append({cols[i]: (cell.get("value") if isinstance(cell, dict) else cell)
                    for i, cell in enumerate(row)})
    return out


def _d(v) -> date | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(v)[:10])
        except ValueError:
            return None


def _occ(symbol: str, exp: date, otype: str, strike: float) -> str:
    cp = "C" if str(otype).lower().startswith("c") else "P"
    return f"{symbol.upper()}{exp:%y%m%d}{cp}{int(round(float(strike) * 1000)):08d}"


def _leg_id(leg: dict) -> str | None:
    sym = leg.get("option_symbol")
    if sym and len(str(sym)) >= 15:
        return str(sym)
    exp = _d(leg.get("expiration_date"))
    if exp is None or leg.get("strike") is None:
        return None
    # infer root from the option_symbol prefix if present, else caller supplies it
    return _occ(leg.get("_symbol", ""), exp, leg.get("option_type", ""), leg["strike"])


def _build_trades(outcomes: list[dict], legs: list[dict]) -> list[LedgerTrade]:
    by_rec: dict[str, list[dict]] = {}
    for leg in legs:
        by_rec.setdefault(leg["recommendation_id"], []).append(leg)
    trades = []
    for o in outcomes:
        rec = o["recommendation_id"]
        rlegs = by_rec.get(rec, [])
        for leg in rlegs:
            leg["_symbol"] = o["symbol"]
        longs = [leg_id for leg in rlegs
                 if str(leg.get("action", "")).lower().startswith("b") or leg.get("leg_role") == "long"
                 if (leg_id := _leg_id(leg))]
        shorts = [leg_id for leg in rlegs
                  if str(leg.get("action", "")).lower().startswith("s") or leg.get("leg_role") == "short"
                  if (leg_id := _leg_id(leg))]
        entry, resolved = _d(o.get("entry_at")), _d(o.get("resolved_at"))
        trades.append(LedgerTrade(
            ref=rec[:10], symbol=o["symbol"], strategy=o.get("strategy_type") or "?",
            entry_date=entry or date(1970, 1, 1), resolved_date=resolved or date(1970, 1, 1),
            long_id=longs[0] if longs else None, short_id=shorts[0] if shorts else None,
            contracts=int(o.get("contracts") or 1),
            recorded_net_pnl=float(o["net_pnl"]) if o.get("net_pnl") is not None else None,
            recorded_outcome=(o.get("outcome") or "").lower() or None,
        ))
    return trades


async def main() -> None:
    url, token = os.environ.get("TURSO_DATABASE_URL"), os.environ.get("TURSO_AUTH_TOKEN")
    if not url or not token:
        print("Set TURSO_DATABASE_URL + TURSO_AUTH_TOKEN (read-only is fine) to run the "
              "cross-check against the real ledger.")
        return

    outcomes = _turso_query(url, token, _OUTCOMES_SQL)
    legs = _turso_query(url, token, _LEGS_SQL)
    trades = _build_trades(outcomes, legs)
    print(f"ledger: {len(trades)} resolved trades\n")

    provider = registry.historical_options_provider()
    cache: dict[str, list] = {}

    async def hist(cid: str | None):
        if not cid:
            return []
        if cid not in cache:
            try:
                cache[cid] = await provider.get_contract_history(cid)
            except Exception as exc:  # noqa: BLE001
                print(f"  fetch failed {cid}: {exc}")
                cache[cid] = []
        return cache[cid]

    rows = []
    for t in trades:
        rows.append(cross_check_trade(t, await hist(t.long_id), await hist(t.short_id)))
    await provider.aclose()

    print(f"{'sym':5} {'strategy':18} {'ledger':>8} {'k=0.5':>8} {'k=1.0':>8}  verdict")
    for r in rows:
        led = f"{r.recorded_pnl:+.0f}" if r.recorded_pnl is not None else "n/a"
        if not r.repriced:
            print(f"{r.symbol:5} {r.strategy:18.18} {led:>8} {'—':>8} {'—':>8}  ({r.note})")
            continue
        a = f"{r.real_mark_pnl_k05:+.0f}"
        b = f"{r.real_mark_pnl_k1:+.0f}"
        verdict = ("FLIP even at mid" if r.sign_flip_optimistic
                   else "slippage-fragile flip" if r.sign_flip
                   else "consistent")
        print(f"{r.symbol:5} {r.strategy:18.18} {led:>8} {a:>8} {b:>8}  {verdict}")

    s = summarize(rows)
    print(f"\nrepriced {s.n_repriced}/{s.n}  |  agree@k1 {s.n_agree}  |  "
          f"flips@k=1.0 {s.n_sign_flip}  |  flips@k=0.5(mid) {s.n_sign_flip_optimistic}")
    for f in s.flips:
        print(f"  ⚑ {f}")
    if s.unrepriced_reasons:
        print("unrepriced:", s.unrepriced_reasons)


if __name__ == "__main__":
    asyncio.run(main())
