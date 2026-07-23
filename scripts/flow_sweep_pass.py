"""flow_sweep power pass — the ONE pre-registered run (docs/flow_sweep_preregistration.md).

Densified entries (every 5 trading days, offsets 25-90) on the existing cached
flow corpus; the 5 FROZEN encodings; walk-forward with train-only orientation;
cluster-bootstrap CI over (symbol, entry-month) at Bonferroni α=0.01; reverse-walk
same-sign check; per-regime sign-flip guard. Hard stopping rule: failures are
REJECTED permanently. Pure cache read; deterministic (seed 12345).

    SCRATCH=/path python -m scripts.flow_sweep_pass
"""

from __future__ import annotations

import json
import os
import random
from collections import defaultdict
from datetime import date, timedelta

from app.backtest.real_mark import evaluate_real_mark_trade
from app.backtest.real_mark_seed import build_engine_verticals, reconstruct_spot_path
from app.backtest.stats import spearman
from app.backtest.walk_forward import walk_forward_folds
from scripts.validate_flow_features import _CACHE, _WIDTH, _load, _parse_occ

_SEED = 12345
_BOOT_ITERS = 5000
_ALPHA = 0.05 / 5  # Bonferroni over the frozen 5-encoding family
_OFFSETS = tuple(range(25, 91, 5))  # densified: 14 entries per (name, expiry)
_N_FOLDS = 4
_EMBARGO = 7
_MIN_FOLD_TEST = 8
_ENCODINGS = ("flow_sweep", "flow_sweep_x_at_ask", "flow_sweep_oi",
              "flow_sweep_burst", "flow_sweep_persist")


def _ratio(b) -> float | None:
    if b.sweep_volume is None or not b.volume:
        return None
    return b.sweep_volume / b.volume


def _encodings(bars, d: date) -> dict[str, float | None]:
    """The five FROZEN encodings, all causal (bars <= entry date d)."""
    hist = [b for b in bars if b.date <= d]
    w5 = hist[-5:]
    out: dict[str, float | None] = dict.fromkeys(_ENCODINGS)
    if not w5:
        return out
    r5 = [r for b in w5 if (r := _ratio(b)) is not None]
    out["flow_sweep"] = sum(r5) / len(r5) if r5 else None
    xs = [
        _ratio(b) * (b.ask_volume / (b.ask_volume + b.bid_volume))
        for b in w5
        if _ratio(b) is not None and b.ask_volume is not None and b.bid_volume is not None
        and (b.ask_volume + b.bid_volume) > 0
    ]
    out["flow_sweep_x_at_ask"] = sum(xs) / len(xs) if xs else None
    svs = [b.sweep_volume for b in w5 if b.sweep_volume is not None]
    ois = [b.open_interest for b in w5 if b.open_interest]
    if svs and ois and sum(ois) > 0:
        out["flow_sweep_oi"] = (sum(svs) / len(svs)) / (sum(ois) / len(ois))
    w20 = [r for b in hist[-20:] if (r := _ratio(b)) is not None]
    entry_r = _ratio(hist[-1]) if hist and hist[-1].date == d else None
    if entry_r is not None and len(w20) >= 10 and sum(w20) > 0:
        out["flow_sweep_burst"] = entry_r / (sum(w20) / len(w20))
    pers = [1 if (r := _ratio(b)) is not None and r > 0 else 0 for b in w5 if _ratio(b) is not None]
    out["flow_sweep_persist"] = sum(pers) / len(pers) if pers else None
    return out


def build_vectors() -> list[dict]:
    groups: dict[tuple[str, date], dict[str, dict[int, list]]] = defaultdict(
        lambda: {"C": {}, "P": {}}
    )
    hist: dict[str, list] = {}
    for fn in os.listdir(_CACHE):
        if not fn.endswith(".json"):
            continue
        cid = fn[:-5]
        parsed = _parse_occ(cid)
        if parsed is None:
            continue
        root, exp, cp, strike = parsed
        if not float(strike).is_integer():
            continue
        bars = _load(cid)
        if not bars:
            continue
        hist[cid] = bars
        groups[(root, exp)][cp][int(strike)] = bars

    vectors: list[dict] = []
    for (root, exp), cp_map in groups.items():
        calls, puts = cp_map["C"], cp_map["P"]
        if len(calls) < 3 or len(puts) < 3:
            continue
        dates = sorted({b.date for v in calls.values() for b in v})
        spot = reconstruct_spot_path(calls, puts, dates)
        if not spot:
            continue
        width = _WIDTH.get(root, 5)
        for trade, rule in build_engine_verticals(
            root, exp, width, calls, puts, spot, entry_offsets=_OFFSETS
        ):
            res = evaluate_real_mark_trade(
                trade, hist.get(trade.long_id, []), hist.get(trade.short_id, []), rule
            )
            if not res.included:
                continue
            k10 = res.by_k.get(1.0)
            if k10 is None or k10.net_pnl_usd is None:
                continue
            enc = _encodings(hist.get(trade.long_id, []), trade.entry_date)
            vectors.append({
                "symbol": root, "entry": trade.entry_date,
                "exit": res.exit_date or (trade.entry_date + timedelta(days=1)),
                "cluster": (root, f"{trade.entry_date:%Y-%m}"),
                "regime": trade.vol_regime or "unknown",
                "y": k10.net_pnl_usd, **enc,
            })
    return vectors


def _oos_pairs(vectors: list[dict], feat: str, *, reverse: bool):
    """Train-oriented pooled OOS (pred, y, regime, cluster) for one walk direction."""
    pooled = []
    folds = walk_forward_folds(
        vectors, lambda v: v["entry"], lambda v: v["exit"],
        n_folds=_N_FOLDS, embargo_days=_EMBARGO, reverse=reverse,
    )
    for fold in folds:
        if len(fold.test) < _MIN_FOLD_TEST:
            continue
        tr = [(v[feat], v["y"]) for v in fold.train if v[feat] is not None]
        if len(tr) < 5:
            continue
        rho = spearman([x for x, _ in tr], [y for _, y in tr])
        if rho is None or rho == 0:
            continue
        s = 1.0 if rho > 0 else -1.0
        pooled.extend(
            (s * v[feat], v["y"], v["regime"], v["cluster"])
            for v in fold.test if v[feat] is not None
        )
    return pooled


def _cluster_boot_rho_ci(pooled, alpha: float):
    """Percentile cluster-bootstrap CI for the pooled OOS Spearman rho: resample
    (symbol, entry-month) clusters — observation-level bootstrap would be
    anticonservative under the densified overlapping entries (pre-registered)."""
    cl: dict[tuple, list[tuple[float, float]]] = defaultdict(list)
    for p, y, _r, c in pooled:
        cl[c].append((p, y))
    keys = sorted(cl)
    if len(keys) < 5:
        return None
    rng = random.Random(_SEED)
    rhos = []
    for _ in range(_BOOT_ITERS):
        pool: list[tuple[float, float]] = []
        for _ in range(len(keys)):
            pool.extend(cl[keys[rng.randrange(len(keys))]])
        rho = spearman([p for p, _ in pool], [y for _, y in pool])
        if rho is not None:
            rhos.append(rho)
    if len(rhos) < 100:
        return None
    rhos.sort()
    return {
        "n_clusters": len(keys),
        "lo": round(rhos[int((alpha / 2) * len(rhos))], 4),
        "hi": round(rhos[int((1 - alpha / 2) * len(rhos)) - 1], 4),
        "alpha": alpha,
    }


def main() -> None:
    vectors = build_vectors()
    with_any = sum(1 for v in vectors if v["flow_sweep"] is not None)
    print(f"[corpus] {len(vectors)} labeled vectors ({with_any} with sweep data) — "
          f"original pass had 305")

    report = {"preregistration": "docs/flow_sweep_preregistration.md", "seed": _SEED,
              "n_vectors": len(vectors), "alpha_bonferroni": _ALPHA, "encodings": {}}
    print(f"\n{'encoding':22} {'n_oos':>5} {'fwd_rho':>8} {'CI(99%)':>18} {'rev_rho':>8} {'flip':>5}  verdict")
    for feat in _ENCODINGS:
        fwd = _oos_pairs(vectors, feat, reverse=False)
        rev = _oos_pairs(vectors, feat, reverse=True)
        fwd_rho = spearman([p for p, _, _, _ in fwd], [y for _, y, _, _ in fwd])
        rev_rho = spearman([p for p, _, _, _ in rev], [y for _, y, _, _ in rev])
        ci = _cluster_boot_rho_ci(fwd, _ALPHA)
        by_regime: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for p, y, r, _c in fwd:
            by_regime[r].append((p, y))
        regime_rhos = {
            r: spearman([p for p, _ in prs], [y for _, y in prs])
            for r, prs in by_regime.items() if len(prs) >= 8
        }
        signs = {1 if v > 0 else -1 for v in regime_rhos.values() if v is not None and abs(v) > 1e-9}
        flip = len(signs) > 1
        validated = (
            ci is not None and ci["lo"] > 0
            and rev_rho is not None and rev_rho >= 0
            and not flip
        )
        report["encodings"][feat] = {
            "n_oos": len(fwd),
            "forward_rho": round(fwd_rho, 4) if fwd_rho is not None else None,
            "cluster_ci": ci,
            "reverse_rho": round(rev_rho, 4) if rev_rho is not None else None,
            "per_regime_rho": {r: round(v, 4) for r, v in regime_rhos.items() if v is not None},
            "regime_sign_flip": flip,
            "verdict": "VALIDATED" if validated else "REJECTED",
        }
        ci_s = f"[{ci['lo']:+.3f},{ci['hi']:+.3f}]" if ci else "—"
        print(f"{feat:22} {len(fwd):>5} "
              f"{fwd_rho:+8.4f}" if fwd_rho is not None else f"{feat:22} {len(fwd):>5} {'—':>8}",
              end="")
        print(f" {ci_s:>18} "
              f"{rev_rho:+8.4f}" if rev_rho is not None else f" {ci_s:>18} {'—':>8}",
              end="")
        print(f" {str(flip)[:1]:>5}  {report['encodings'][feat]['verdict']}")

    with open(os.path.join("docs", "flow_sweep_pass_report.json"), "w") as f:
        json.dump(report, f, indent=2, default=str)
    print("\n[report] wrote docs/flow_sweep_pass_report.json")


if __name__ == "__main__":
    main()
