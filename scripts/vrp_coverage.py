"""VRP experiment §1 — IV-history coverage report (BLOCKING precondition).

Measures, never assumes: per-symbol × per-era density of USABLE tenor-matched IV
observations across the cached per-contract UW history, plus the FMP daily-close
reach that the realized-vol side needs. The pre-registered regime set is written
FROM this table (vrp_experiment_spec §1).

Why per-contract bars: the UW underlying-level iv-rank series is hard-capped at 1Y
regardless of the requested timespan (probed live 2026-07-23), so it cannot reach
the 2022 bear. Per-contract bars are also the tenor-honest source — a contract's IV
at DTE≈h IS the expiry-specific IV for horizon h (§3), no term-structure guessing.

A "usable observation" for (symbol, day d, horizon h):
  * some cached expiry has DTE(d) inside the tenor band for h (30d: 25-35, 45d: 40-50),
  * parity spot is reconstructable on d,
  * a call within ±3% moneyness of spot has a recorded IV on d,
  * and the forward RV window [d, d+h] lies inside the FMP daily-close range.

Pure cache read + one FMP price-history call per symbol. No UW API calls.

    SCRATCH=/path python -m scripts.vrp_coverage
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from datetime import date, timedelta

from app.backtest.real_mark_seed import reconstruct_spot_path
from app.domain.historic import HistoricOptionBar
from app.providers import registry

_SCRATCH = os.environ.get("SCRATCH", "/tmp")
_CACHES = (os.path.join(_SCRATCH, "hist_cache"), os.path.join(_SCRATCH, "flow_cache"))

# Tenor bands per horizon (§3: IV tenor must match the realization window).
_BANDS = {30: (25, 35), 45: (40, 50)}
_MONEYNESS = 0.03

# Era boundaries (§1 table rows).
_ERAS = [
    ("2021_meltup", date(2021, 1, 1), date(2021, 12, 31)),
    ("2022_bear", date(2022, 1, 1), date(2022, 12, 31)),
    ("2023_24_recovery", date(2023, 1, 1), date(2024, 12, 31)),
    ("2025_drawdown_chop", date(2025, 1, 1), date(2025, 12, 31)),
    ("2026_ytd", date(2026, 1, 1), date(2026, 12, 31)),
]

# Usable-density threshold (declared BEFORE counting): an era cell is usable when
# >= 3 symbols each carry >= 20 tenor-matched observations in that era.
_MIN_SYMBOLS = 3
_MIN_OBS_PER_SYMBOL = 20


def _era_of(d: date) -> str | None:
    for name, lo, hi in _ERAS:
        if lo <= d <= hi:
            return name
    return None


def _parse_occ(cid: str) -> tuple[str, date, str, float] | None:
    if len(cid) < 16:
        return None
    tail = cid[-15:]
    root = cid[:-15]
    try:
        exp = date(2000 + int(tail[0:2]), int(tail[2:4]), int(tail[4:6]))
        cp = tail[6]
        strike = int(tail[7:]) / 1000
    except (ValueError, IndexError):
        return None
    return (root, exp, cp, strike) if cp in ("C", "P") and root else None


def _load(path: str, cid: str) -> list[HistoricOptionBar]:
    """Schema-tolerant loader for both cache formats (compact + verbose)."""
    rows = json.load(open(path))
    out: list[HistoricOptionBar] = []
    for r in rows:
        if "d" in r:  # compact (flow_experiment)
            out.append(HistoricOptionBar(
                contract_id=cid, date=date.fromisoformat(r["d"]), nbbo_bid=r["b"],
                nbbo_ask=r["a"], iv=r["iv"], open_interest=r["oi"], volume=r["v"],
            ))
        elif "date" in r:  # verbose (real_mark_backtest)
            out.append(HistoricOptionBar(
                contract_id=cid, date=date.fromisoformat(r["date"]), nbbo_bid=r.get("bid"),
                nbbo_ask=r.get("ask"), iv=r.get("iv"), open_interest=r.get("oi"),
                volume=r.get("vol"),
            ))
    return out


def load_groups() -> dict[tuple[str, date], dict[str, dict[float, list[HistoricOptionBar]]]]:
    """(root, expiry) -> {"C": {strike: bars}, "P": {strike: bars}} across all caches.
    flow_cache wins on collisions (it carries the same pricing fields plus flow)."""
    groups: dict = defaultdict(lambda: {"C": {}, "P": {}})
    for cache in _CACHES:
        if not os.path.isdir(cache):
            continue
        for fn in os.listdir(cache):
            if not fn.endswith(".json"):
                continue
            cid = fn[:-5]
            parsed = _parse_occ(cid)
            if parsed is None:
                continue
            root, exp, cp, strike = parsed
            bars = _load(os.path.join(cache, fn), cid)
            if bars:
                groups[(root, exp)][cp][strike] = bars
    return groups


async def fmp_ranges(symbols: list[str]) -> dict[str, tuple[date, date] | None]:
    md = registry.market_data_provider()
    out: dict[str, tuple[date, date] | None] = {}
    for s in sorted(symbols):
        try:
            h = await md.get_price_history(s, lookback_days=2000)
            cs = h.candles if h else []
            out[s] = (cs[0].ts.date(), cs[-1].ts.date()) if cs else None
        except Exception as exc:  # noqa: BLE001 — a symbol miss is itself a coverage fact
            print(f"[fmp] {s}: {type(exc).__name__}: {exc}")
            out[s] = None
    return out


def main() -> None:
    groups = load_groups()
    symbols = sorted({root for (root, _exp) in groups})
    print(f"[cache] {len(groups)} (name,expiry) groups across {len(symbols)} symbols")

    fmp = asyncio.run(fmp_ranges(symbols))

    # counts[h][era][symbol] = set of usable observation dates
    counts: dict[int, dict[str, dict[str, set]]] = {
        h: defaultdict(lambda: defaultdict(set)) for h in _BANDS
    }
    for (root, exp), cp_map in groups.items():
        calls, puts = cp_map["C"], cp_map["P"]
        if len(calls) < 3 or len(puts) < 3:
            continue
        dates = sorted({b.date for v in calls.values() for b in v})
        spot_path = reconstruct_spot_path(calls, puts, dates)
        rng = fmp.get(root)
        for d in dates:
            spot = spot_path.get(d)
            if not spot:
                continue
            dte = (exp - d).days
            for h, (lo, hi) in _BANDS.items():
                if not (lo <= dte <= hi):
                    continue
                # RV window must be computable from FMP closes.
                if rng is None or d < rng[0] or d + timedelta(days=h) > rng[1]:
                    continue
                era = _era_of(d)
                if era is None:
                    continue
                atm_ok = any(
                    abs(k - spot) / spot <= _MONEYNESS
                    and any(b.date == d and b.iv for b in bars)
                    for k, bars in calls.items()
                )
                if atm_ok:
                    counts[h][era][root].add(d)

    report: dict = {
        "generated_from": "hist_cache + flow_cache per-contract UW bars (disk), FMP closes",
        "underlying_iv_series_note": (
            "UW /iv-rank underlying series is capped at 1Y regardless of timespan "
            "(probed 2026-07-23) — it cannot serve the 2021/2022 eras. Per-contract "
            "bars are the only IV source reaching 2021, and are tenor-honest."
        ),
        "fmp_ranges": {s: ([str(r[0]), str(r[1])] if r else None) for s, r in fmp.items()},
        "threshold": {"min_symbols": _MIN_SYMBOLS, "min_obs_per_symbol": _MIN_OBS_PER_SYMBOL},
        "horizons": {},
    }
    for h in _BANDS:
        rows = []
        for era, _lo, _hi in _ERAS:
            per_sym = {s: len(ds) for s, ds in sorted(counts[h][era].items())}
            qualifying = [s for s, n in per_sym.items() if n >= _MIN_OBS_PER_SYMBOL]
            ns = sorted(per_sym.values())
            median = ns[len(ns) // 2] if ns else 0
            verdict = "USABLE" if len(qualifying) >= _MIN_SYMBOLS else "INSUFFICIENT"
            rows.append({
                "era": era, "symbols_with_data": len(per_sym),
                "symbols_meeting_min_obs": len(qualifying),
                "median_obs_per_symbol": median, "total_obs": sum(ns),
                "per_symbol": per_sym, "verdict": verdict,
            })
        report["horizons"][str(h)] = rows

    os.makedirs("docs", exist_ok=True)
    out_path = os.path.join("docs", "vrp_coverage_report.json")
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)

    for h in _BANDS:
        print(f"\n===== horizon {h}d (tenor band {_BANDS[h][0]}-{_BANDS[h][1]} DTE) =====")
        print(f"{'era':20} {'syms':>5} {'>=min':>6} {'med':>5} {'total':>6}  verdict")
        for row in report["horizons"][str(h)]:
            print(f"{row['era']:20} {row['symbols_with_data']:>5} "
                  f"{row['symbols_meeting_min_obs']:>6} {row['median_obs_per_symbol']:>5} "
                  f"{row['total_obs']:>6}  {row['verdict']}")
    print(f"\n[report] wrote {out_path}")


if __name__ == "__main__":
    main()
