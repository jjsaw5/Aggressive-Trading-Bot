"""VRP Stage 2 — harvestability at k=1.0, exactly as pre-registered.

Implements docs/vrp_stage2_preregistration.md (committed before this ran): the
frozen 6-variant grid (unconditional / causal iv-rich × put-credit / call-credit /
iron-condor) at h=45, first-in-band entries, 5%-OTM short legs one ladder step
wide, fixed exits (PT 50% credit / stop 2x credit / 21td), real NBBO fills at
k=1.0 (k=0.5 reported for slippage-fragility) with commissions, liquidity guard,
per-era + tail reporting, cluster bootstrap with Bonferroni over 6 variants.

Pure cache read. Deterministic (seed 12345).

    SCRATCH=/path python -m scripts.vrp_stage2
"""

from __future__ import annotations

import json
import os
import random
from collections import defaultdict
from datetime import date

from app.backtest.real_mark import ExitRule, RealMarkTrade, evaluate_real_mark_trade
from app.backtest.real_mark_seed import occ, reconstruct_spot_path
from scripts.vrp_coverage import _era_of, load_groups
from scripts.vrp_stage1 import _MIN_PRIOR_OBS

_SEED = 12345
_BOOT_ITERS = 5000
_N_VARIANTS = 6
_ALPHA = 0.05 / _N_VARIANTS  # Bonferroni over the frozen grid
_MATERIAL_USD = 5.0
_BAND = (40, 50)
_OTM = 0.05
_EXIT = ExitRule(profit_target_pct=0.5, stop_loss_pct=1.0, time_stop_days=21)
_IN_SCOPE = ("2022_bear", "2023_24_recovery", "2025_drawdown_chop")
_RICH_CUT = 2 / 3


def _trailing_pct_at(bars, d: date) -> float | None:
    ivs = sorted(((b.date, b.iv) for b in bars if b.iv), key=lambda t: t[0])
    hist = [iv for dd, iv in ivs if dd <= d]
    if len(hist) < _MIN_PRIOR_OBS:
        return None
    return sum(1 for v in hist if v <= hist[-1]) / len(hist)


def _pick_short_long(strikes: list[int], spot: float, side: str) -> tuple[int, int] | None:
    """5%-OTM short + one-ladder-step-further long. side: 'put' | 'call'."""
    if side == "put":
        below = sorted([k for k in strikes if k <= spot * (1 - _OTM)])
        if len(below) < 2:
            return None
        return below[-1], below[-2]  # (short, long) — long is further OTM
    above = sorted([k for k in strikes if k >= spot * (1 + _OTM)])
    if len(above) < 2:
        return None
    return above[0], above[1]


def build_population() -> dict[str, list[tuple[RealMarkTrade, dict]]]:
    """variant -> [(trade, meta)] per the frozen grid. Iron condor entries are the
    paired PCS+CCS trades sharing a `pair` id (P&L summed at reporting)."""
    groups = load_groups()
    out: dict[str, list] = defaultdict(list)
    for (root, exp), cp_map in groups.items():
        calls, puts = cp_map["C"], cp_map["P"]
        if len(calls) < 3 or len(puts) < 3:
            continue
        dates = sorted({b.date for v in calls.values() for b in v})
        spot_path = reconstruct_spot_path(calls, puts, dates)
        entry = next(
            (d for d in dates
             if _BAND[0] <= (exp - d).days <= _BAND[1] and spot_path.get(d)
             and _era_of(d) in _IN_SCOPE),
            None,
        )
        if entry is None:
            continue
        spot = spot_path[entry]
        era = _era_of(entry)
        # Causal iv-rich condition from the near-ATM call (Stage-1 construct).
        atm_ok = [k for k in calls if abs(k - spot) / spot <= 0.03]
        rich = None
        if atm_ok:
            atm_k = min(atm_ok, key=lambda x: abs(x - spot))
            rich = _trailing_pct_at(calls[atm_k], entry)
        dte = (exp - entry).days
        meta = {"symbol": root, "date": str(entry), "era": era, "rich": rich}

        legs = {}
        pk = _pick_short_long(sorted(puts), spot, "put")
        ck = _pick_short_long(sorted(calls), spot, "call")
        if pk:
            short_k, long_k = pk
            legs["put_credit_spread"] = RealMarkTrade(
                trade_id=f"{root}:{exp:%y%m%d}:pcs:{entry}",
                long_id=occ(root, exp, "P", long_k), short_id=occ(root, exp, "P", short_k),
                entry_date=entry, dte_at_entry=dte, strategy="put_credit_spread",
                direction="bullish", vol_regime=era,
            )
        if ck:
            short_k, long_k = ck
            legs["call_credit_spread"] = RealMarkTrade(
                trade_id=f"{root}:{exp:%y%m%d}:ccs:{entry}",
                long_id=occ(root, exp, "C", long_k), short_id=occ(root, exp, "C", short_k),
                entry_date=entry, dte_at_entry=dte, strategy="call_credit_spread",
                direction="bearish", vol_regime=era,
            )
        for cond, ok in (("unconditional", True), ("iv_rich", rich is not None and rich >= _RICH_CUT)):
            if not ok:
                continue
            for s in ("put_credit_spread", "call_credit_spread"):
                if s in legs:
                    out[f"{cond}:{s}"].append((legs[s], meta))
            if "put_credit_spread" in legs and "call_credit_spread" in legs:
                pair = f"{root}:{exp:%y%m%d}"
                for s in ("put_credit_spread", "call_credit_spread"):
                    out[f"{cond}:iron_condor"].append((legs[s], {**meta, "pair": pair}))
    return out


def _evaluate(variant_rows, hist):
    """(meta, k05_pnl, k10_pnl) per entry — iron-condor pairs summed."""
    per_entry: dict[str, dict] = {}
    excluded = defaultdict(int)
    for trade, meta in variant_rows:
        res = evaluate_real_mark_trade(
            trade, hist.get(trade.long_id, []), hist.get(trade.short_id, []), _EXIT
        )
        if not res.included:
            excluded[res.exclusion_reason or "?"] += 1
            continue
        k05 = res.by_k.get(0.5)
        k10 = res.by_k.get(1.0)
        if k10 is None or k10.net_pnl_usd is None:
            excluded["unfillable_k1"] += 1
            continue
        key = meta.get("pair") or trade.trade_id
        slot = per_entry.setdefault(key, {"meta": meta, "k05": 0.0, "k10": 0.0, "n": 0})
        slot["k10"] += k10.net_pnl_usd
        slot["k05"] += k05.net_pnl_usd if (k05 and k05.net_pnl_usd is not None) else 0.0
        slot["n"] += 1
    # An iron condor with only one fillable wing is excluded (not half-counted).
    rows = [v for v in per_entry.values() if "pair" not in v["meta"] or v["n"] == 2]
    excluded["ic_one_wing"] += sum(1 for v in per_entry.values() if "pair" in v["meta"] and v["n"] != 2)
    return rows, dict(excluded)


def _boot_ci(rows, key, alpha):
    cl = defaultdict(list)
    for r in rows:
        cl[(r["meta"]["symbol"], r["meta"]["date"][:7])].append(r[key])
    keys = sorted(cl)
    if len(keys) < 5:
        return None
    rng = random.Random(_SEED)
    means = []
    for _ in range(_BOOT_ITERS):
        pool = []
        for _ in range(len(keys)):
            pool.extend(cl[keys[rng.randrange(len(keys))]])
        means.append(sum(pool) / len(pool))
    means.sort()
    lo = means[int((alpha / 2) * len(means))]
    hi = means[int((1 - alpha / 2) * len(means)) - 1]
    return {"n_clusters": len(keys), "lo": round(lo, 2), "hi": round(hi, 2), "alpha": round(alpha, 5)}


def _tail_block(pnls_by_date):
    pnls = [p for _, p in pnls_by_date]
    if not pnls:
        return {}
    s = sorted(pnls)
    n = len(s)
    cvar_n = max(1, int(0.05 * n))
    equity = peak = dd = 0.0
    for _, p in sorted(pnls_by_date, key=lambda t: t[0]):
        equity += p
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    loss_months = defaultdict(float)
    for d, p in pnls_by_date:
        if p < 0:
            loss_months[d[:7]] += p
    worst_months = sorted(loss_months.items(), key=lambda t: t[1])[:3]
    return {
        "max_drawdown_usd": round(dd, 2), "worst_trade_usd": round(s[0], 2),
        "cvar_5pct_usd": round(sum(s[:cvar_n]) / cvar_n, 2),
        "worst_loss_months": [[m, round(v, 2)] for m, v in worst_months],
    }


def main() -> None:
    groups = load_groups()
    hist: dict[str, list] = {}
    for (_root, _exp), cp_map in groups.items():
        for side in ("C", "P"):
            for _k, bars in cp_map[side].items():
                if bars:
                    hist[bars[0].contract_id] = bars

    population = build_population()
    report: dict = {"preregistration": "docs/vrp_stage2_preregistration.md",
                    "seed": _SEED, "alpha_bonferroni": _ALPHA, "variants": {}}
    print(f"{'variant':32} {'n':>4} {'win%':>5} {'exp@1.0':>8} {'exp@0.5':>8} {'PF':>5}  CI(mean@1.0)")
    for variant in sorted(population):
        rows, excluded = _evaluate(population[variant], hist)
        if not rows:
            report["variants"][variant] = {"n": 0, "excluded": excluded}
            continue
        k10 = [r["k10"] for r in rows]
        k05 = [r["k05"] for r in rows]
        wins = sum(1 for p in k10 if p > 0)
        losses = sum(1 for p in k10 if p < 0)
        gains = sum(p for p in k10 if p > 0)
        pain = -sum(p for p in k10 if p < 0)
        exp10 = sum(k10) / len(k10)
        exp05 = sum(k05) / len(k05)
        ci = _boot_ci(rows, "k10", _ALPHA)
        by_era = {}
        for era in _IN_SCOPE:
            sub = [r["k10"] for r in rows if r["meta"]["era"] == era]
            if sub:
                by_era[era] = {"n": len(sub), "expectancy_usd": round(sum(sub) / len(sub), 2),
                               "net_usd": round(sum(sub), 2)}
        regimes_with_wins = {r["meta"]["era"] for r in rows if r["k10"] > 0}
        degenerate = losses < 2 or len(regimes_with_wins) <= 1
        tail = _tail_block([(r["meta"]["date"], r["k10"]) for r in rows])
        passes = (
            not degenerate and ci is not None and ci["lo"] > 0
            and exp10 > _MATERIAL_USD
            and by_era.get("2022_bear", {}).get("n", 0) > 0 and losses >= 2
        )
        report["variants"][variant] = {
            "n": len(rows), "wins": wins, "losses": losses,
            "win_rate": round(wins / len(rows), 4),
            "expectancy_k10_usd": round(exp10, 2), "expectancy_k05_usd": round(exp05, 2),
            "net_k10_usd": round(sum(k10), 2),
            "profit_factor_k10": round(gains / pain, 3) if pain else None,
            "slippage_fragile": exp05 > 0 >= exp10,
            "ci_mean_k10": ci, "by_era": by_era, "tail": tail,
            "degenerate_uncalibratable": degenerate, "excluded": excluded,
            "PASSES_PREREG": passes,
        }
        pf = f"{gains / pain:.2f}" if pain else "inf"
        ci_s = f"[{ci['lo']:+.0f},{ci['hi']:+.0f}]" if ci else "—"
        print(f"{variant:32} {len(rows):>4} {100 * wins / len(rows):>4.0f}% "
              f"{exp10:>+8.2f} {exp05:>+8.2f} {pf:>5}  {ci_s}"
              f"{'  DEGENERATE' if degenerate else ''}{'  PASSES' if passes else ''}")
        bear = by_era.get("2022_bear")
        if bear:
            print(f"{'':32} 2022_bear: n={bear['n']} exp={bear['expectancy_usd']:+.2f} "
                  f"net={bear['net_usd']:+.2f} | maxDD={tail['max_drawdown_usd']:.0f} "
                  f"worst={tail['worst_trade_usd']:.0f} CVaR5={tail['cvar_5pct_usd']:.0f}")

    with open(os.path.join("docs", "vrp_stage2_report.json"), "w") as f:
        json.dump(report, f, indent=2)
    print("\n[report] wrote docs/vrp_stage2_report.json")


if __name__ == "__main__":
    main()
