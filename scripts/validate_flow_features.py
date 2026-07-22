"""Test the app's PREMISE: does options flow predict net-of-cost outcomes?

The price/vol/structure/cost features did not validate (docs/FEATURE_VALIDATION_RESULT.md).
But flow — at-ask aggression, sweeps, premium, unusual volume, OI building — is the
app's actual thesis, and it was untested there (no flow in the pricing corpus, and
none pre-2023 in the UW tier). This builds a FLOW-BEARING corpus for the 2023+ eras
(flow fields are populated from 2023 on), extracts the flow features on the option
being BOUGHT, and runs the SAME leak-safe harness over pricing + flow features
together (correct Bonferroni family).

A separate flow cache persists the flow fields the pricing cache dropped.

    UW_HISTORIC_ENABLED=true UNUSUAL_WHALES_API_KEY=… \
        python -m scripts.validate_flow_features
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import date

from app.backtest.feature_validation import build_registry
from app.backtest.features import ALL_FEATURES, FLOW_FEATURES, FeatureVector, extract_features
from app.backtest.real_mark import evaluate_real_mark_trade
from app.backtest.real_mark_seed import (
    build_engine_verticals,
    near_atm,
    occ,
    reconstruct_spot_path,
)
from app.domain.historic import HistoricOptionBar
from app.providers import registry as prov_registry
from scripts.real_mark_backtest import _PRESETS

# 2023+ only — the eras where UW historic flow fields are populated.
_FLOW_ERAS = ("2023-24", "2025-26")
_CACHE = os.path.join(os.environ.get("SCRATCH", "/tmp"), "flow_cache")
_REGISTRY_PATH = os.path.join("docs", "flow_feature_registry.json")


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
            ask_volume=r.get("ask_v"), bid_volume=r.get("bid_v"),
            sweep_volume=r.get("sweep_v"), total_premium=r.get("prem"),
        )
        for r in rows
    ]


def _save(cid: str, bars: list[HistoricOptionBar]) -> None:
    os.makedirs(_CACHE, exist_ok=True)
    json.dump(
        [{"date": b.date.isoformat(), "bid": b.nbbo_bid, "ask": b.nbbo_ask, "iv": b.iv,
          "oi": b.open_interest, "vol": b.volume, "trades": b.trades,
          "ask_v": b.ask_volume, "bid_v": b.bid_volume, "sweep_v": b.sweep_volume,
          "prem": b.total_premium}
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


def _era_feature_vectors(universe, expiries, hist) -> list[FeatureVector]:
    out: list[FeatureVector] = []
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
            call_strikes = sorted(calls)
            for trade, rule in build_engine_verticals(root, exp, width, calls, puts, spot):
                result = evaluate_real_mark_trade(
                    trade, hist.get(trade.long_id, []), hist.get(trade.short_id, []), rule
                )
                spot_at_entry = spot.get(trade.entry_date)
                atm_k = near_atm(call_strikes, spot_at_entry) if spot_at_entry else None
                atm_call_bars = calls.get(atm_k, []) if atm_k is not None else []
                fv = extract_features(trade, result, hist=hist, spot_path=spot, atm_call_bars=atm_call_bars)
                if fv is not None:
                    out.append(fv)
    return out


async def main() -> None:
    provider = prov_registry.historical_options_provider()
    vectors: list[FeatureVector] = []
    for p in _FLOW_ERAS:
        universe, expiries = _PRESETS[p]
        ids = {
            occ(root, exp, cp, k)
            for root, strikes, _ in universe
            for exp in expiries
            for cp in "CP"
            for k in strikes
        }
        hist = await _fetch_all(provider, ids)
        era_vecs = _era_feature_vectors(universe, expiries, hist)
        with_flow = sum(1 for fv in era_vecs if fv.values.get("flow_at_ask") is not None)
        print(f"[era {p}] {len(era_vecs)} vectors, {with_flow} with flow features")
        vectors.extend(era_vecs)
    await provider.aclose()

    features = ALL_FEATURES + FLOW_FEATURES
    reg = build_registry(
        vectors, features=features,
        corpus_note=f"FLOW corpus, eras={','.join(_FLOW_ERAS)}, pricing+flow features, net-of-cost k=1.0.",
    )
    print(f"\n===== FLOW FEATURE VALIDATION · n={reg.n_trades} · {len(features)} features =====")
    print(json.dumps(reg.as_dict(), indent=2, default=str))

    os.makedirs("docs", exist_ok=True)
    with open(_REGISTRY_PATH, "w") as f:
        json.dump(reg.as_dict(), f, indent=2, default=str)
    print(f"\n[registry] wrote {_REGISTRY_PATH} — "
          f"{'validated: ' + ', '.join(reg.weights) if reg.any_validated else 'NO feature validated (honest null)'}")


if __name__ == "__main__":
    argparse.ArgumentParser().parse_args()
    asyncio.run(main())
