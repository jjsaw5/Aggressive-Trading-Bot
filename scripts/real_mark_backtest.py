"""Scaled real-mark backtest against live UW per-contract history.

Fetches option-contract histories (disk-cached so re-runs are instant), builds a
multi-name / multi-expiry / multi-entry population, reprices every leg from
recorded NBBO, and prints the aggregate report — in two modes:

    fixed   systematic call-debit / put-credit / put-debit verticals
    engine  the scanner's own selection rule chooses the trade (§2)

Run (needs the UW historic entitlement):
    UW_HISTORIC_ENABLED=true UNUSUAL_WHALES_API_KEY=... \
        python -m scripts.real_mark_backtest --mode both
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import date

from app.backtest.real_mark import evaluate_real_mark_trade
from app.backtest.real_mark_runner import aggregate
from app.backtest.real_mark_seed import (
    build_engine_verticals,
    build_fixed_verticals,
    monthly_expiries,
    occ,
    reconstruct_spot_path,
)
from app.domain.historic import HistoricOptionBar
from app.providers import registry

_CACHE = os.path.join(
    os.environ.get("SCRATCH", "/tmp"), "hist_cache"
)

# (root, strike ladder, vertical width) per era — clean names, no split in window.
_PRESETS = {
    "2021-22": (
        [("SPY", list(range(340, 481, 20)), 20),
         ("QQQ", list(range(280, 401, 20)), 20),
         ("AAPL", list(range(120, 191, 10)), 10),
         ("MSFT", list(range(220, 341, 20)), 20)],
        monthly_expiries(date(2021, 10, 1), date(2022, 12, 31))[::2],
    ),
    "2023-24": (  # a non-crisis regime: prices higher, trend mostly up
        [("SPY", list(range(380, 601, 20)), 20),
         ("QQQ", list(range(280, 501, 20)), 20),
         ("AAPL", list(range(140, 241, 10)), 10),
         ("MSFT", list(range(240, 461, 20)), 20)],
        monthly_expiries(date(2023, 3, 1), date(2024, 6, 30))[::3],
    ),
}


def _cache_path(cid: str) -> str:
    return os.path.join(_CACHE, f"{cid}.json")


def _load(cid: str) -> list[HistoricOptionBar] | None:
    p = _cache_path(cid)
    if not os.path.exists(p):
        return None
    rows = json.load(open(p))
    return [
        HistoricOptionBar(
            contract_id=cid, date=date.fromisoformat(r["date"]),
            nbbo_bid=r["bid"], nbbo_ask=r["ask"], iv=r["iv"],
            open_interest=r["oi"], volume=r["vol"], trades=r["trades"],
        )
        for r in rows
    ]


def _save(cid: str, bars: list[HistoricOptionBar]) -> None:
    os.makedirs(_CACHE, exist_ok=True)
    json.dump(
        [{"date": b.date.isoformat(), "bid": b.nbbo_bid, "ask": b.nbbo_ask,
          "iv": b.iv, "oi": b.open_interest, "vol": b.volume, "trades": b.trades}
         for b in bars],
        open(_cache_path(cid), "w"),
    )


async def _fetch_all(provider, ids: set[str]) -> dict[str, list[HistoricOptionBar]]:
    hist: dict[str, list[HistoricOptionBar]] = {}
    fetched = 0
    for cid in sorted(ids):
        cached = _load(cid)
        if cached is not None:
            hist[cid] = cached
            continue
        bars = await provider.get_contract_history(cid)
        _save(cid, bars)
        hist[cid] = bars
        fetched += 1
    print(f"[fetch] {len(ids)} contracts ({fetched} live, {len(ids)-fetched} cached)")
    return hist


async def main(mode: str, preset: str) -> None:
    universe, expiries = _PRESETS[preset]
    provider = registry.historical_options_provider()

    ids: set[str] = set()
    for root, strikes, _ in universe:
        for exp in expiries:
            for cp in "CP":
                for k in strikes:
                    ids.add(occ(root, exp, cp, k))

    hist = await _fetch_all(provider, ids)
    await provider.aclose()

    for m in (["fixed", "engine"] if mode == "both" else [mode]):
        builder = build_fixed_verticals if m == "fixed" else build_engine_verticals
        trades = []
        for root, strikes, width in universe:
            for exp in expiries:
                calls = {k: hist.get(occ(root, exp, "C", k), []) for k in strikes}
                puts = {k: hist.get(occ(root, exp, "P", k), []) for k in strikes}
                calls = {k: v for k, v in calls.items() if v}
                puts = {k: v for k, v in puts.items() if v}
                if len(calls) < 3 or len(puts) < 3:
                    continue
                dates = sorted({b.date for v in calls.values() for b in v})
                spot = reconstruct_spot_path(calls, puts, dates)
                trades += builder(root, exp, width, calls, puts, spot)

        pairs = []
        for trade, rule in trades:
            lb = hist.get(trade.long_id, [])
            sb = hist.get(trade.short_id, [])
            pairs.append((trade, evaluate_real_mark_trade(trade, lb, sb, rule)))
        report = aggregate(pairs)
        print(f"\n===== MODE: {m} =====")
        print(json.dumps(report.as_dict(), indent=2, default=str))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["fixed", "engine", "both"], default="both")
    ap.add_argument("--preset", choices=list(_PRESETS), default="2021-22")
    args = ap.parse_args()
    asyncio.run(main(args.mode, args.preset))
