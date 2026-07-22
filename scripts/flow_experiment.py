"""Flow-alpha experiment — free-data run (2023-2026), spring-2025 stress fold.

Per the pre-registration + Amendment 1. Auto-centers a fine strike ladder per
(name, expiry) via a coarse parity probe (no multi-year price feed exists), builds
engine-selected verticals, reprices them at real bid/ask (k=0.5 and k=1.0),
reconstructs the EOD flow proxy from the same chain, tags each trade CONFIRM /
NEUTRAL / OPPOSE, and runs the walk-forward verdict with April-2025 as the
mandatory stress fold. Disk-cached; resumable.

    UW_HISTORIC_ENABLED=true UNUSUAL_WHALES_API_KEY=... SCRATCH=/path \
        python -m scripts.flow_experiment
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from datetime import date

from app.backtest.flow_experiment import ExpTrade, run_experiment
from app.backtest.flow_proxy import FlowThresholds, aggregate_day, features
from app.backtest.real_mark import evaluate_real_mark_trade
from app.backtest.real_mark_seed import build_engine_verticals, occ, reconstruct_spot_path
from app.domain.historic import HistoricOptionBar
from app.providers import registry

_CACHE = os.path.join(os.environ.get("SCRATCH", "/tmp"), "flow_cache")
_STRESS = (date(2025, 2, 15), date(2025, 4, 30))

# (name, coarse_lo, coarse_hi, fine_step) — coarse span covers the 2023-26 range for
# parity spot; fine_step is a realistic vertical width. 25 liquid, no-split names.
_UNIVERSE = [
    ("SPY", 360, 640, 10), ("QQQ", 280, 580, 10), ("IWM", 160, 260, 5), ("DIA", 320, 460, 10),
    ("AAPL", 130, 280, 5), ("MSFT", 230, 540, 10), ("META", 120, 780, 20), ("AMZN", 85, 250, 5),
    ("GOOGL", 85, 220, 5), ("NFLX", 280, 1000, 20), ("TSLA", 140, 480, 10), ("AMD", 80, 240, 5),
    ("JPM", 130, 320, 5), ("BAC", 26, 52, 1), ("XOM", 95, 130, 2.5), ("HD", 270, 460, 10),
    ("DIS", 80, 125, 2.5), ("KO", 55, 76, 1), ("PEP", 140, 190, 5), ("COST", 480, 1050, 20),
    ("ORCL", 90, 260, 5), ("CRM", 190, 400, 10), ("INTC", 18, 52, 1), ("UNH", 240, 630, 10),
    ("V", 220, 380, 5),
]
_EXPIRIES = [date(2023, 6, 16), date(2023, 12, 15), date(2024, 6, 21), date(2024, 12, 20),
             date(2025, 3, 21), date(2025, 6, 20), date(2025, 12, 19)]
_ENTRY_OFFSETS = (25, 40, 60)
_GRID = [FlowThresholds(lean=a, sweep=b, prem=c)
         for a, b, c in itertools.product((0.05, 0.10, 0.15, 0.20), (0.10, 0.20, 0.30), (0.5, 1.0, 1.5))]


def _cp(x: float) -> str:
    return f"{x:g}"


def _cache_path(cid: str) -> str:
    return os.path.join(_CACHE, f"{cid}.json")


def _load(cid: str) -> list[HistoricOptionBar] | None:
    p = _cache_path(cid)
    if not os.path.exists(p):
        return None
    otype = cid[-15:][6] if len(cid) >= 15 else None
    return [HistoricOptionBar(
        contract_id=cid, date=date.fromisoformat(r["d"]), nbbo_bid=r["b"], nbbo_ask=r["a"],
        iv=r["iv"], open_interest=r["oi"], volume=r["v"], ask_volume=r["av"], bid_volume=r["bv"],
        sweep_volume=r["sv"], total_premium=r["tp"], option_type=otype,
    ) for r in json.load(open(p))]


def _save(cid: str, bars: list[HistoricOptionBar]) -> None:
    os.makedirs(_CACHE, exist_ok=True)
    json.dump([{"d": b.date.isoformat(), "b": b.nbbo_bid, "a": b.nbbo_ask, "iv": b.iv,
                "oi": b.open_interest, "v": b.volume, "av": b.ask_volume, "bv": b.bid_volume,
                "sv": b.sweep_volume, "tp": b.total_premium} for b in bars], open(_cache_path(cid), "w"))


async def _hist(provider, cid: str, cache: dict) -> list[HistoricOptionBar]:
    if cid in cache:
        return cache[cid]
    bars = _load(cid)
    if bars is None:
        try:
            bars = await provider.get_contract_history(cid)
        except Exception:
            bars = []
        _save(cid, bars)
    cache[cid] = bars
    return bars


def _regime(d: date) -> str:
    if d.year == 2023:
        return "2023_recovery"
    if d.year == 2024:
        return "2024_bull"
    if _STRESS[0] <= d <= _STRESS[1]:
        return "2025_drawdown"
    if d.year == 2025:
        return "2025_chop"
    return "2026"


async def main() -> None:
    provider = registry.historical_options_provider()
    cache: dict[str, list] = {}
    exp_trades: list[ExpTrade] = []
    fetched = 0

    for name, lo, hi, step in _UNIVERSE:
        coarse = [round(lo + (hi - lo) * i / 4) for i in range(5)]
        for exp in _EXPIRIES:
            # 1) coarse probe -> spot path via parity
            coarse_bars = {}
            for k in coarse:
                for cp in "CP":
                    coarse_bars[(cp, k)] = await _hist(provider, occ(name, exp, cp, k), cache)
            fetched += 10
            cdict = {k: coarse_bars[("C", k)] for k in coarse if coarse_bars[("C", k)]}
            pdict = {k: coarse_bars[("P", k)] for k in coarse if coarse_bars[("P", k)]}
            if len(cdict) < 2 or len(pdict) < 2:
                continue
            dates = sorted({b.date for v in cdict.values() for b in v})
            spot_path = reconstruct_spot_path(cdict, pdict, dates)
            if not spot_path:
                continue
            # 2) fine ATM ladder centered on spot ~40TD before expiry
            ref = sorted(spot_path)[-41] if len(spot_path) > 41 else sorted(spot_path)[0]
            center = spot_path[ref]
            fine = [round((center + (j - 3) * step) / step) * step for j in range(7)]
            fine = [s for s in fine if s > 0]
            fbars_c, fbars_p = {}, {}
            for k in fine:
                fbars_c[k] = await _hist(provider, occ(name, exp, "C", k), cache)
                fbars_p[k] = await _hist(provider, occ(name, exp, "P", k), cache)
            fetched += len(fine) * 2
            fbars_c = {k: v for k, v in fbars_c.items() if v}
            fbars_p = {k: v for k, v in fbars_p.items() if v}
            if len(fbars_c) < 3 or len(fbars_p) < 3:
                continue

            # flow proxy: aggregate the whole fetched chain per day
            all_by_date: dict[date, list] = {}
            for v in list(fbars_c.values()) + list(fbars_p.values()) + list(cdict.values()) + list(pdict.values()):
                for b in v:
                    all_by_date.setdefault(b.date, []).append(b)
            raw_by_date = {d: aggregate_day(bs) for d, bs in all_by_date.items()}

            width = step
            trades = build_engine_verticals(name, exp, width, fbars_c, fbars_p, spot_path, _ENTRY_OFFSETS)
            for trade, rule in trades:
                res = evaluate_real_mark_trade(
                    trade, cache.get(trade.long_id, []), cache.get(trade.short_id, []), rule)
                if not res.included:
                    continue
                k05, k1 = res.by_k.get(0.5), res.by_k.get(1.0)
                if not (k05 and k1 and k05.fillable and k1.fillable):
                    continue
                exp_trades.append(ExpTrade(
                    entry_date=trade.entry_date,
                    exit_date=res.exit_date or trade.entry_date,
                    direction=trade.direction, regime=_regime(trade.entry_date),
                    net_by_k={0.5: k05.net_pnl_usd, 1.0: k1.net_pnl_usd},
                    flow=features(raw_by_date, trade.entry_date),
                ))
        print(f"[{name}] cumulative trades={len(exp_trades)} fetched≈{fetched}", flush=True)

    await provider.aclose()

    result = run_experiment(exp_trades, _GRID, n_folds=4, embargo_days=55,
                            material_margin=15.0, stress_window=_STRESS)
    from dataclasses import asdict
    out = asdict(result)
    out["grid_summary"] = vars(result.grid_summary)
    out["pooled_ci"] = vars(result.pooled_ci) if result.pooled_ci else None
    out["fold_results"] = [{"index": fr.index, "test_spread": fr.test_spread,
                            "best_thr": vars(fr.best_thr) if fr.best_thr else None} for fr in result.fold_results]
    # regime/arm census for the report
    from app.backtest.flow_proxy import flow_arm
    census: dict = {}
    thr0 = _GRID[0]
    for t in exp_trades:
        arm = flow_arm(t.flow, t.direction, thr0)
        census.setdefault(t.regime, {}).setdefault(arm, 0)
        census[t.regime][arm] += 1
    out["census_regime_arm_thr0"] = census
    print("\n===== FLOW EXPERIMENT RESULT =====")
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
