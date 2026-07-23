# VRP Experiment — Pre-Registration

**Committed BEFORE any VRP number was computed.** Per `vrp_experiment_spec.md` v2.
The regime set below is derived from the §1 coverage report
(`docs/vrp_coverage_report.json`), generated first, not from assumption.

## 0. Preconditions — status at registration

- **§1 coverage check: PASSED with re-scope.** Verified per-symbol × per-era density
  from the cached per-contract UW bars (the UW *underlying* iv-rank series is
  hard-capped at 1Y regardless of requested timespan — probed live 2026-07-23 — so
  per-contract bars are the only IV source reaching 2021-22). FMP dailies reach
  2021-07-26.
- **§4 `_iv_rank_proxy` causality: SETTLED (trailing-only).** Verified in code during
  the audit round of 2026-07-23: the percentile is computed over
  `[b.iv for b in bars if b.date <= entry]` — no full-series, no look-ahead. The
  n=218 pricing validation needed no re-run. Stage 2's causality gate is therefore
  already satisfied; Stage 2 remains gated only on Stage 1 being positive.

## 1. Regime set (from the coverage table — the only editable input was the threshold, declared before counting: ≥3 symbols with ≥20 obs each)

| Era | 30d cell | 45d cell | In scope? |
|---|---|---|---|
| 2021 melt-up | 5 syms, 0 meet min, 70 obs | 5 syms, 0 meet min, 80 obs | **EXCLUDED — insufficient** |
| **2022 bear** | 5 syms, **5 meet min**, 157 obs | 5 syms, **5 meet min**, 140 obs | **IN (the decisive cell)** |
| 2023-24 recovery | 25 syms, 21 meet min, 701 obs | 25 syms, 22 meet min, 768 obs | IN |
| 2025 drawdown+chop | 25 syms, 5 meet min, 443 obs | 25 syms, 15 meet min, 563 obs | IN |
| 2026 YTD | 5 syms, 0 meet min, 35 obs | 5 syms, 0 meet min, 35 obs | **EXCLUDED — insufficient** |

The 2022 bear is present — the spec's stop-and-re-scope trigger did NOT fire. The
bear cell is 5 names (SPY, QQQ, IWM, AAPL, MSFT — index-heavy); this width is
recorded as a known limit, not hidden. Excluding 2021/2026 is a coverage fact.

## 2. Universe, horizons, sampling

- **Universe:** the 25 cached liquid names (union of hist_cache + flow_cache);
  2022 cell limited to the 5 above.
- **Horizons:** h = 30 and h = 45 calendar days, run separately. 0DTE / short-DTE
  are excluded — EOD data cannot measure intraday RV (spec §3/§8).
- **Sampling:** every trading day where some cached expiry has DTE inside the tenor
  band (30d: 25-35 DTE; 45d: 40-50 DTE). Overlapping RV windows induce serial
  dependence, so **inference uses a cluster bootstrap resampling (symbol, entry-month)
  clusters** (seeded, deterministic), never i.i.d. resampling of observations.

## 3. Measurement definitions (frozen)

- **IV(t, h):** the recorded IV of the near-ATM call (|strike−spot|/spot ≤ 3%,
  nearest strike wins) of the expiry whose DTE lies in the tenor band, on entry day
  t. Tenor = actual DTE; assert |DTE − h| ≤ 5. Construct label:
  `per_contract_atm_iv` (expiry-specific — tenor-matched by construction).
- **Spot:** put-call parity reconstruction from the same chain (existing corpus
  machinery).
- **RV(t → t+h):** close-to-close log returns from FMP dailies over trading days in
  (t, t + h calendar]; annualized `std(returns, ddof=1) × √252`. Minimum sample:
  ≥ 0.8 × the expected trading-day count (h30: ≥17; h45: ≥25), else the observation
  is dropped (dropped counts reported).
- **Both forms:** `vrp_vol = IV − RV` and `vrp_variance = IV² − RV²`, both reported.
- **Trailing IV percentile (conditioning):** percentile of entry-day IV within the
  contract's own trailing series (min 10 prior obs) — the `_iv_rank_proxy`
  construct, causal, labeled `contract_trailing_percentile`. This is NOT the
  underlying 1Y IV rank; the construct is recorded on every result.
- **Term structure:** per-day slope is mostly uncomputable from monthly-spaced
  cached expiries; in its place the mandatory **§3 tenor-sensitivity check** is run:
  recompute "VRP" with deliberately mismatched tenor (45d-band IV against 30d RV
  windows, and 30d-band IV against 45d RV windows) and report how much apparent
  premium mismatch alone manufactures.

## 4. Hypotheses

- **H1a (Stage 1):** pooled `vrp_vol` > 0 with cluster-bootstrap 95% CI excluding
  zero, AND pooled mean `vrp_vol` ≥ **2.0 vol points** (the pre-registered material
  margin), AND the premium is not explained by tenor mismatch.
- **H1b (Stage 2, only if H1a):** defined-risk short-premium structures net positive
  at k=1.0 out of sample, tail paid (2022 + 2025 cells with real losses), per spec
  §6/§9.
- **H0 (default, expected):** premium absent, or present but consumed by costs/tail.

**Prior evidence (spec §7, recorded here so it cannot be rationalized later):** the
forward-ledger cross-check already ran a miniature Stage 2 — the META/AMD
put-credit-spread book was 10-0 at mid and **0-10 on real marks at k=1.0**; six flips
were pure slippage. n≈10, degenerate, not a verdict — but directionally consistent
with every finding this engagement has produced. Stage-2 expectation is explicitly
LOW. Stage 1 is expected positive (VRP is well documented); a large Stage-2 positive
would be treated as a bug until proven otherwise.

## 5. Stage-1 report contents (fixed before running)

Pooled and per-era: n, mean, median, sd of both VRP forms; left tail (5th/1st
percentiles, worst observations and their dates/clustering); per trailing-IV-
percentile bucket (terciles); time-decay check (2022 vs 2023-24 vs 2025 means);
tenor-sensitivity check; dropped-observation accounting. Deterministic (seeded
bootstrap, seed=12345).

## 6. Stage-1 → Stage-2 gate

Proceed to Stage 2 only if **all** of H1a holds. Stage-2 protocol (structures
put/call credit spreads + iron condors only; k=1.0 both legs + commissions;
pre-registered exits PT=50% of credit, stop=2× credit, time stop=21 trading days;
walk-forward purge+embargo both directions; grid-median reporting with
multiple-comparisons correction; §6.1 tail characterization mandatory) is frozen as
written in the spec and will get its own pre-registered variant grid before running.

## 7. Product note (spec §8)

This experiment tests 30-45 DTE credit structures. **A positive result would NOT
validate the 0DTE/1-5DTE product as built** — it would argue for a swing credit
book, a materially different product. A swing-horizon finding must not be read as a
green light for the short-duration path.

## 8. Config (frozen)

```
VRP_UNIVERSE=25 cached names (2022 cell: SPY,QQQ,IWM,AAPL,MSFT)
VRP_START=2022-01-01            # from §1 — 2021 excluded, insufficient density
VRP_REGIMES=2022_bear,2023_24_recovery,2025_drawdown_chop
VRP_HORIZONS=30,45
VRP_TENOR_BANDS=25-35,40-50     # assert |DTE − h| ≤ 5
VRP_IV_CONSTRUCT=per_contract_atm_iv (moneyness ≤ 3%)
VRP_RANK_CONSTRUCT=contract_trailing_percentile (causal, min 10 prior obs)
VRP_RV=close_to_close_log, sqrt(252), min 0.8×expected trading days
VRP_BOOTSTRAP=cluster (symbol, entry-month), seed=12345, iters=5000
VRP_MATERIAL_MARGIN_VOLPTS=2.0
VRP_FILL_K=1.0                  # Stage 2
VRP_STRUCTURES=put_credit_spread,call_credit_spread,iron_condor   # Stage 2
```

*Research and decision-support only — not investment advice.*
