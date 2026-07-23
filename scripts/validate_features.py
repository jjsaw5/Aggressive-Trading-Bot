"""Run the feature-validation harness against the real-mark corpus.

Builds the engine-selected trade population across the same split-free eras the
corpus uses (disk-cached, resumable), reprices each from recorded UW NBBO,
extracts causal entry-time features, and asks — per feature, out-of-sample,
net-of-cost — does it predict? Prints the verdicts and writes the data-derived
weight registry to docs/feature_registry.json.

    UW_HISTORIC_ENABLED=true UNUSUAL_WHALES_API_KEY=… \
        python -m scripts.validate_features --preset all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from app.backtest.feature_validation import build_registry
from app.backtest.features import FeatureVector, extract_features
from app.backtest.real_mark import evaluate_real_mark_trade
from app.backtest.real_mark_seed import (
    build_engine_verticals,
    near_atm,
    occ,
    reconstruct_spot_path,
)
from app.providers import registry as prov_registry

# Reuse the corpus presets + disk cache so this never re-fetches what the backtest
# already pulled (same SCRATCH/hist_cache location).
from scripts.real_mark_backtest import _PRESETS, _fetch_all

_REGISTRY_PATH = os.path.join("docs", "feature_registry.json")


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
                fv = extract_features(
                    trade, result, hist=hist, spot_path=spot, atm_call_bars=atm_call_bars
                )
                if fv is not None:
                    out.append(fv)
    return out


async def main(preset: str) -> None:
    presets = list(_PRESETS) if preset == "all" else [preset]
    provider = prov_registry.historical_options_provider()

    vectors: list[FeatureVector] = []
    for p in presets:
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
        print(f"[era {p}] {len(era_vecs)} labeled feature vectors")
        vectors.extend(era_vecs)
    await provider.aclose()

    reg = build_registry(
        vectors,
        corpus_note=(
            f"engine-selected corpus, eras={','.join(presets)}, "
            f"net-of-cost label at k=1.0 (full spread)."
        ),
    )
    print(f"\n===== FEATURE VALIDATION · n={reg.n_trades} · alpha={reg.alpha} =====")
    print(json.dumps(reg.as_dict(), indent=2, default=str))

    os.makedirs("docs", exist_ok=True)
    with open(_REGISTRY_PATH, "w") as f:
        json.dump(reg.as_dict(), f, indent=2, default=str)
    print(f"\n[registry] wrote {_REGISTRY_PATH} — "
          f"{'validated: ' + ', '.join(reg.weights) if reg.any_validated else 'NO feature validated (honest null)'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", choices=[*_PRESETS, "all"], default="all")
    args = ap.parse_args()
    asyncio.run(main(args.preset))
