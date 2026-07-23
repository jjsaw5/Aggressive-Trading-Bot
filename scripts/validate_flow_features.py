"""Test the app's PREMISE: does options flow predict net-of-cost outcomes?

The price/vol/structure/cost features did not validate (docs/FEATURE_VALIDATION_RESULT.md).
But flow — at-ask aggression, sweeps, premium, unusual volume, OI building — is the
app's actual thesis, and it was untested there (no flow in the pricing corpus, and
none pre-2023 in the UW tier).

This reuses the flow-bearing corpus the flow experiment already fetched (a 25-name,
2023-2025 cache with real UW flow fields — at/bid volume, sweeps, premium), rebuilds
the engine-selected verticals from it, extracts the flow features on the option being
BOUGHT, and runs the SAME leak-safe harness over pricing + flow features together
(correct Bonferroni family). Pure cache read — no re-fetch. If the flow_cache is
absent, run `python -m scripts.flow_experiment` first to populate it.

    SCRATCH=/path python -m scripts.validate_flow_features
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import date

from app.backtest.feature_validation import build_registry
from app.backtest.features import ALL_FEATURES, FLOW_FEATURES, FeatureVector, extract_features
from app.backtest.real_mark import evaluate_real_mark_trade
from app.backtest.real_mark_seed import build_engine_verticals, near_atm, reconstruct_spot_path
from app.domain.historic import HistoricOptionBar
from scripts.flow_experiment import _UNIVERSE

_CACHE = os.path.join(os.environ.get("SCRATCH", "/tmp"), "flow_cache")
_REGISTRY_PATH = os.path.join("docs", "flow_feature_registry.json")
_WIDTH = {root: int(w) if float(w).is_integer() else w for root, _lo, _hi, w in _UNIVERSE}


def _load(cid: str) -> list[HistoricOptionBar]:
    """Schema-tolerant loader. The flow cache uses the compact keys written by
    scripts.flow_experiment (d/b/a/iv/oi/v/av/bv/sv/tp); tolerate the verbose keys
    too so a mixed cache still loads. A row matching neither is skipped."""
    otype = cid[-15:][6] if len(cid) >= 15 else None
    rows = json.load(open(os.path.join(_CACHE, f"{cid}.json")))
    out: list[HistoricOptionBar] = []
    for r in rows:
        if "d" in r:  # compact schema (flow_experiment)
            out.append(HistoricOptionBar(
                contract_id=cid, date=date.fromisoformat(r["d"]), nbbo_bid=r["b"], nbbo_ask=r["a"],
                iv=r["iv"], open_interest=r["oi"], volume=r["v"], ask_volume=r["av"],
                bid_volume=r["bv"], sweep_volume=r["sv"], total_premium=r["tp"], option_type=otype,
            ))
        elif "date" in r:  # verbose schema (pricing-only; no flow fields)
            out.append(HistoricOptionBar(
                contract_id=cid, date=date.fromisoformat(r["date"]), nbbo_bid=r.get("bid"),
                nbbo_ask=r.get("ask"), iv=r.get("iv"), open_interest=r.get("oi"),
                volume=r.get("vol"), option_type=otype,
            ))
    return out


def _parse_occ(cid: str) -> tuple[str, date, str, float] | None:
    """`{ROOT}{yymmdd}{C|P}{strike*1000:08d}` -> (root, exp, cp, strike)."""
    if len(cid) < 16:
        return None
    tail = cid[-15:]
    root = cid[: -15]
    try:
        exp = date(2000 + int(tail[0:2]), int(tail[2:4]), int(tail[4:6]))
        cp = tail[6]
        strike = int(tail[7:]) / 1000
    except (ValueError, IndexError):
        return None
    if cp not in ("C", "P") or not root:
        return None
    return root, exp, cp, strike


def main() -> None:
    if not os.path.isdir(_CACHE):
        raise SystemExit(f"no flow cache at {_CACHE}; run `python -m scripts.flow_experiment` first.")

    # Group cached contracts by (root, expiry), splitting calls/puts by strike.
    groups: dict[tuple[str, date], dict[str, dict[float, list]]] = defaultdict(
        lambda: {"C": {}, "P": {}}
    )
    hist: dict[str, list[HistoricOptionBar]] = {}
    files = [fn for fn in os.listdir(_CACHE) if fn.endswith(".json")]
    for fn in files:
        cid = fn[:-5]
        parsed = _parse_occ(cid)
        if parsed is None:
            continue
        root, exp, cp, strike = parsed
        bars = _load(cid)
        if not bars:
            continue
        hist[cid] = bars
        groups[(root, exp)][cp][strike] = bars
    print(f"[cache] {len(files)} files, {len(hist)} non-empty contracts, {len(groups)} (name,expiry) groups")

    vectors: list[FeatureVector] = []
    for (root, exp), cp_map in groups.items():
        calls, puts = cp_map["C"], cp_map["P"]
        if len(calls) < 3 or len(puts) < 3:
            continue
        width = _WIDTH.get(root, 5)
        dates = sorted({b.date for v in calls.values() for b in v})
        spot = reconstruct_spot_path(calls, puts, dates)
        if not spot:
            continue
        # near_atm/occ need int strikes to reconstruct OCC ids the builder looks up.
        int_calls = {int(k): v for k, v in calls.items() if float(k).is_integer()}
        int_puts = {int(k): v for k, v in puts.items() if float(k).is_integer()}
        if len(int_calls) < 3 or len(int_puts) < 3:
            continue
        for trade, rule in build_engine_verticals(root, exp, width, int_calls, int_puts, spot):
            result = evaluate_real_mark_trade(
                trade, hist.get(trade.long_id, []), hist.get(trade.short_id, []), rule
            )
            spot_at_entry = spot.get(trade.entry_date)
            atm_k = near_atm(sorted(int_calls), spot_at_entry) if spot_at_entry else None
            atm_call_bars = int_calls.get(atm_k, []) if atm_k is not None else []
            fv = extract_features(trade, result, hist=hist, spot_path=spot, atm_call_bars=atm_call_bars)
            if fv is not None:
                vectors.append(fv)

    with_flow = sum(1 for fv in vectors if fv.values.get("flow_at_ask") is not None)
    print(f"[corpus] {len(vectors)} labeled vectors, {with_flow} with flow features")

    features = ALL_FEATURES + FLOW_FEATURES
    reg = build_registry(
        vectors, features=features,
        corpus_note="FLOW corpus (flow_experiment cache, 25 names, 2023-2025), pricing+flow, net-of-cost k=1.0.",
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
    main()
