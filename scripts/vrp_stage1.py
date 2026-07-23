"""VRP Stage 1 — premium existence, exactly as pre-registered.

Implements docs/vrp_preregistration.md (committed before this ran): tenor-matched
per-contract ATM IV vs forward realized vol, both VRP forms, per-regime split,
causal trailing-percentile conditioning, left tail, time-decay, and the mandatory
tenor-sensitivity check. Deterministic (cluster bootstrap, seed=12345).

Pure cache read + one FMP price-history call per symbol.

    SCRATCH=/path python -m scripts.vrp_stage1
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
from collections import defaultdict
from datetime import date, timedelta

from app.providers import registry
from scripts.vrp_coverage import _BANDS, _MONEYNESS, _era_of, load_groups

_SEED = 12345
_BOOT_ITERS = 5000
_MATERIAL_MARGIN = 2.0  # vol points, pre-registered
_MIN_RETURNS = {30: 17, 45: 25}  # >= 0.8 x expected trading days
_MIN_PRIOR_OBS = 10  # trailing-percentile conditioning (causal)
_IN_SCOPE_ERAS = ("2022_bear", "2023_24_recovery", "2025_drawdown_chop")


def _rv(closes: list[tuple[date, float]], entry: date, h: int) -> tuple[float | None, int]:
    """Annualized close-to-close RV over trading days in (entry, entry+h calendar].
    Base close = last trading day <= entry. Returns (rv, n_returns)."""
    end = entry + timedelta(days=h)
    base = None
    window: list[float] = []
    for d, c in closes:
        if d <= entry:
            base = c
        elif d <= end:
            window.append(c)
        else:
            break
    if base is None or len(window) < 2:
        return None, len(window)
    series = [base, *window]
    rets = [math.log(series[i + 1] / series[i]) for i in range(len(series) - 1)]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(252.0), n


def _trailing_pct(ivs_by_date: list[tuple[date, float]], entry: date) -> float | None:
    """Causal trailing percentile of the entry-day IV within the contract's own
    prior series (construct: contract_trailing_percentile)."""
    hist = [iv for d, iv in ivs_by_date if d <= entry]
    if len(hist) < _MIN_PRIOR_OBS:
        return None
    cur = hist[-1]
    return sum(1 for v in hist if v <= cur) / len(hist)


def _stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    s = sorted(vals)
    n = len(s)
    mean = sum(s) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in s) / (n - 1)) if n > 1 else 0.0
    q = lambda p: s[min(n - 1, int(p * n))]  # noqa: E731
    return {
        "n": n, "mean": round(mean, 4), "median": round(q(0.5), 4), "sd": round(sd, 4),
        "p05": round(q(0.05), 4), "p01": round(q(0.01), 4),
        "min": round(s[0], 4), "max": round(s[-1], 4),
    }


def _cluster_boot_ci(rows: list[dict], key: str) -> dict | None:
    """95% cluster-bootstrap CI for the mean of rows[key], clusters=(symbol, month)."""
    clusters: dict[tuple, list[float]] = defaultdict(list)
    for r in rows:
        clusters[(r["symbol"], r["date"][:7])].append(r[key])
    keys = sorted(clusters)
    if len(keys) < 5:
        return None
    rng = random.Random(_SEED)
    means = []
    for _ in range(_BOOT_ITERS):
        pool: list[float] = []
        for _ in range(len(keys)):
            pool.extend(clusters[keys[rng.randrange(len(keys))]])
        means.append(sum(pool) / len(pool))
    means.sort()
    return {
        "n_clusters": len(keys),
        "lo95": round(means[int(0.025 * len(means))], 4),
        "hi95": round(means[int(0.975 * len(means)) - 1], 4),
    }


def main() -> None:
    groups = load_groups()
    symbols = sorted({root for (root, _e) in groups})

    async def _closes() -> dict[str, list[tuple[date, float]]]:
        md = registry.market_data_provider()
        out = {}
        for s in symbols:
            try:
                h = await md.get_price_history(s, lookback_days=2000)
                out[s] = [(c.ts.date(), c.close) for c in (h.candles if h else [])]
            except Exception as exc:  # noqa: BLE001
                print(f"[fmp] {s}: {exc}")
                out[s] = []
        return out

    closes = asyncio.run(_closes())

    rows: list[dict] = []
    dropped = defaultdict(int)
    for (root, exp), cp_map in groups.items():
        calls, puts = cp_map["C"], cp_map["P"]
        if len(calls) < 3 or len(puts) < 3:
            continue
        from app.backtest.real_mark_seed import reconstruct_spot_path
        dates = sorted({b.date for v in calls.values() for b in v})
        spot_path = reconstruct_spot_path(calls, puts, dates)
        # Per-strike IV series (date-sorted) for trailing percentiles.
        iv_series = {
            k: sorted(((b.date, b.iv) for b in bars if b.iv), key=lambda t: t[0])
            for k, bars in calls.items()
        }
        for d in dates:
            era = _era_of(d)
            if era not in _IN_SCOPE_ERAS:
                continue
            spot = spot_path.get(d)
            if not spot:
                dropped["no_spot"] += 1
                continue
            dte = (exp - d).days
            for h, (lo, hi) in _BANDS.items():
                if not (lo <= dte <= hi):
                    continue
                assert abs(dte - h) <= 5, "tenor-match assertion (pre-registered)"
                near = [k for k in calls if abs(k - spot) / spot <= _MONEYNESS]
                if not near:
                    dropped[f"no_atm_h{h}"] += 1
                    continue
                k = min(near, key=lambda x: abs(x - spot))
                iv = next((v for dd, v in iv_series.get(k, []) if dd == d), None)
                if not iv:
                    dropped[f"no_iv_h{h}"] += 1
                    continue
                rv, n_ret = _rv(closes.get(root, []), d, h)
                if rv is None or n_ret < _MIN_RETURNS[h]:
                    dropped[f"rv_short_h{h}"] += 1
                    continue
                # Mismatched-tenor RV for the §3 sensitivity check.
                h_mis = 45 if h == 30 else 30
                rv_mis, n_mis = _rv(closes.get(root, []), d, h_mis)
                if rv_mis is not None and n_mis < _MIN_RETURNS[h_mis]:
                    rv_mis = None
                rows.append({
                    "symbol": root, "date": str(d), "era": era, "h": h, "dte": dte,
                    "strike": k, "moneyness": round((k - spot) / spot, 4),
                    "iv": round(iv, 4), "rv": round(rv, 4),
                    "vrp_vol": round(iv - rv, 4),
                    "vrp_variance": round(iv * iv - rv * rv, 4),
                    "rv_mismatched": round(rv_mis, 4) if rv_mis is not None else None,
                    "trailing_pct": _trailing_pct(iv_series.get(k, []), d),
                })

    report: dict = {"preregistration": "docs/vrp_preregistration.md", "seed": _SEED,
                    "dropped": dict(dropped), "horizons": {}}
    for h in _BANDS:
        hr = [r for r in rows if r["h"] == h]
        vv = [r["vrp_vol"] for r in hr]
        pooled = _stats(vv)
        pooled["mean_vol_points"] = round(pooled.get("mean", 0) * 100, 2) if hr else None
        ci = _cluster_boot_ci(hr, "vrp_vol")
        by_era = {
            era: {
                "vrp_vol": _stats([r["vrp_vol"] for r in hr if r["era"] == era]),
                "vrp_variance": _stats([r["vrp_variance"] for r in hr if r["era"] == era]),
            }
            for era in _IN_SCOPE_ERAS
        }
        # Fixed thirds of the causal trailing-percentile scale.
        buckets = {}
        for label, plo, phi in (("low_0_33", 0.0, 1 / 3), ("mid_33_67", 1 / 3, 2 / 3),
                                ("high_67_100", 2 / 3, 1.0001)):
            sub = [r["vrp_vol"] for r in hr
                   if r["trailing_pct"] is not None and plo <= r["trailing_pct"] < phi]
            buckets[label] = _stats(sub)
        worst = sorted(hr, key=lambda r: r["vrp_vol"])[:10]
        # §3 tenor-sensitivity: matched mean vs the mean this horizon's IV would show
        # against the OTHER horizon's RV window — the premium mismatch manufactures.
        mis_rows = [r for r in hr if r["rv_mismatched"] is not None]
        mis_mean = (
            round(sum(r["iv"] - r["rv_mismatched"] for r in mis_rows) / len(mis_rows), 4)
            if mis_rows else None
        )
        report["horizons"][str(h)] = {
            "pooled_vrp_vol": pooled, "cluster_boot_ci_mean_vrp_vol": ci,
            "pooled_vrp_variance": _stats([r["vrp_variance"] for r in hr]),
            "by_era": by_era, "by_trailing_pct_bucket": buckets,
            "worst_10": worst,
            "tenor_sensitivity": {
                "matched_mean_vrp_vol": pooled.get("mean"),
                "mismatched_mean_vrp_vol": mis_mean,
                "n_mismatched": len(mis_rows),
                "mismatch_manufactures": (
                    round(mis_mean - pooled["mean"], 4)
                    if (mis_mean is not None and hr) else None
                ),
            },
        }

    # Pre-registered H1a gate, evaluated per horizon.
    gates = {}
    for h in _BANDS:
        rep = report["horizons"][str(h)]
        ci = rep["cluster_boot_ci_mean_vrp_vol"]
        mean_pts = (rep["pooled_vrp_vol"].get("mean") or 0) * 100
        gates[str(h)] = {
            "ci_excludes_zero": bool(ci and ci["lo95"] > 0),
            "material_margin_met": mean_pts >= _MATERIAL_MARGIN,
            "mean_vol_points": round(mean_pts, 2),
        }
    report["h1a_gate"] = gates

    os.makedirs("docs", exist_ok=True)
    with open(os.path.join("docs", "vrp_stage1_report.json"), "w") as f:
        json.dump(report, f, indent=2)

    for h in _BANDS:
        rep = report["horizons"][str(h)]
        p = rep["pooled_vrp_vol"]
        ci = rep["cluster_boot_ci_mean_vrp_vol"]
        print(f"\n===== STAGE 1 · h={h}d · n={p.get('n', 0)} =====")
        if p.get("n"):
            print(f"pooled vrp_vol: mean={p['mean']:+.4f} ({p['mean']*100:+.1f} vol pts) "
                  f"median={p['median']:+.4f} sd={p['sd']:.4f}")
            print(f"left tail: p05={p['p05']:+.4f} p01={p['p01']:+.4f} min={p['min']:+.4f}")
            if ci:
                print(f"cluster-boot 95% CI (mean): [{ci['lo95']:+.4f}, {ci['hi95']:+.4f}] "
                      f"({ci['n_clusters']} clusters)")
            for era in _IN_SCOPE_ERAS:
                e = rep["by_era"][era]["vrp_vol"]
                if e.get("n"):
                    print(f"  {era:20} n={e['n']:>4} mean={e['mean']:+.4f} "
                          f"p05={e['p05']:+.4f} min={e['min']:+.4f}")
            ts = rep["tenor_sensitivity"]
            print(f"tenor check: matched={ts['matched_mean_vrp_vol']:+.4f} "
                  f"mismatched={ts['mismatched_mean_vrp_vol']} "
                  f"(mismatch alone manufactures {ts['mismatch_manufactures']})")
        print(f"H1a gate: {report['h1a_gate'][str(h)]}")
    print(f"\ndropped: {dict(dropped)}")
    print("[report] wrote docs/vrp_stage1_report.json")


if __name__ == "__main__":
    main()
