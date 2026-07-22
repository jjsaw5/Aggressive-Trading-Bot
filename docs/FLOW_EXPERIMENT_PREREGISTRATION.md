# Pre-Registration — Does Flow Add Alpha Beyond Beta?

**Frozen before any results exist.** Per the experiment spec §4.1, this commits the
hypothesis, features, gate, arms, threshold grid, primary metric, floors, and
success criteria in advance. Any change after results are seen is a **new**
experiment, disclosed as such. The purpose of the experiment is to *try to
disprove* a flow edge; a credible null is a successful outcome, not a failure.

---

## 0. flow_source — determined up front

**`flow_source = proxy_eod`.** Verified 2026-07-22: the UW API tier exposes no
historical flow-alerts (the `/api/stock/{ticker}/flow-alerts` and
`/api/option-trades/flow-alerts` endpoints ignore all date parameters and return
only real-time alerts; the sole historical-flow archive is a separate paid parquet
product we do not hold). Therefore the experiment runs on the reconstructed EOD
proxy (§2b of the spec).

**Standing caveat, carried into every result:** a proxy result **motivates but does
not validate** the live flow signal (the proxy is EOD and lacks the opening-trade
and single-vs-multi-leg discrimination the live alerts carry). A proxy *positive*
requires re-confirmation on true flow-alerts before any capital; a proxy *null* is
informative but not fully conclusive against the richer live signal.

## 1. Hypothesis

- **H1:** Among trades the trend skeleton already selects, those where the flow
  proxy *confirms* the direction earn higher net-of-cost expectancy than those
  where flow *opposes* it — the differential surviving out-of-sample, within
  (regime × direction), at k=1.0.
- **H0 (default, expected):** CONFIRM and OPPOSE perform the same net of costs.
  We fail to reject H0 unless the evidence clears every §5 bar.

This is a **differential** claim (CONFIRM − OPPOSE within a cell), never
"flow-filtered trades are profitable" (that is beta).

## 2. Flow features (proxy_eod)

Per (underlying, date), aggregated over a near-ATM chain subset (the strikes
within ±`CHAIN_ATM_BAND` of the reconstructed spot, front `CHAIN_N_EXPIRIES`
expiries), from the `/api/option-contract/{id}/historic` daily fields:

```
at_ask_lean  = (Σ ask_volume − Σ bid_volume) / Σ(ask_volume + bid_volume)   ∈ [−1, 1]
sweep_frac   = Σ sweep_volume / Σ volume
premium_z    = zscore(Σ total_premium  vs  trailing-20d)
net_call_put = (Σ call total_premium − Σ put total_premium) / Σ total_premium ∈ [−1, 1]
voloi        = Σ volume / Σ open_interest            # reported, not gated
```

A day with insufficient chain coverage (< `MIN_CHAIN_CONTRACTS` usable) yields NO
flow read → the trade is NEUTRAL by construction (never guessed).

## 3. The gate (boolean, never a score pillar)

```
flow_bull_score      = at_ask_lean + net_call_put          # >0 bullish, <0 bearish
directional_agree    = (flow_bull_score > 0 and dir==BULLISH) or (flow_bull_score < 0 and dir==BEARISH)
magnitude_sufficient = abs(at_ask_lean) >= θ_lean AND (sweep_frac >= θ_sweep OR premium_z >= θ_prem)

CONFIRM : directional_agree AND magnitude_sufficient
OPPOSE  : (NOT directional_agree) AND magnitude_sufficient    # actively against, with conviction
NEUTRAL : otherwise (incl. no flow read)
```

Flow stays a gate until this experiment clears; only then is it eligible for the
shadow-metric promotion protocol (real-loss floor + positive lift + degeneracy
clear) already built.

## 4. Threshold grid (tuned on TRAIN folds only, frozen before TEST)

```
θ_lean  ∈ {0.05, 0.10, 0.15, 0.20}
θ_sweep ∈ {0.10, 0.20, 0.30}
θ_prem  ∈ {0.5, 1.0, 1.5}          # z-score units
```

36 combinations. The out-of-sample lift **distribution** across all 36 is reported
(median, not argmax); any significance claim carries a Bonferroni correction over
36 (α = 0.05/36 ≈ 0.00139), or an equivalent permutation family-wise threshold.

## 5. Primary metric + arms

- **Primary:** CONFIRM − OPPOSE **net-expectancy spread (USD/trade)**, computed
  **within each (regime × direction) cell** then pooled, at **k = 1.0**, resolved
  at real bid/ask via the real-mark evaluator. Never mid, never theoretical.
- Secondary: CONFIRM − NEUTRAL, and CONFIRM − ungated baseline.
- Reported per arm: n, win rate, expectancy, PF, max DD, at k=0.5 **and** k=1.0.
- Headline: the pooled CONFIRM − OPPOSE spread with a **bootstrap 95% CI**.

## 6. Out-of-sample protocol (frozen)

- **Universe precondition (§4.2):** ≥ 25 liquid names, ≥ 4 years, ≥ 3 distinct
  regimes (2021 melt-up, H1-2022 bear, 2023–24 recovery, + a chop/range stretch).
  **No verdict is reported on a universe below this floor** — an under-powered run
  is reported as `insufficient`, not as evidence.
- **Walk-forward** with rolling folds: tune θ on each train fold, evaluate frozen
  on the next test fold, roll. **Purge** trades whose holding period straddles a
  train/test boundary; **embargo** ≥ `EMBARGO_DAYS` (≥ max DTE, 55) between train
  and test. Aggregate over test folds only.
- **Both-direction robustness:** require the CONFIRM − OPPOSE lift to hold with the
  **same sign** in every test fold and in both walk directions (train-early/test-late
  and train-late/test-early).
- **Loss floor / degeneracy:** each arm in each pooled cell needs ≥ 2 real losses
  and > 1 regime, else `uncalibratable`.

## 7. Pre-registered success criteria (all must hold)

Flow is declared a **candidate** edge (motivating, not capital-worthy) only if:

1. CONFIRM − OPPOSE net-expectancy spread is **positive within every tested
   regime** (same sign, no reversal), pooled and per-regime.
2. The pooled spread at **k = 1.0** exceeds `MATERIAL_MARGIN_USD` with a bootstrap
   95% CI excluding zero, **after** the multiple-comparisons correction.
3. Both arms clear the loss floor; neither trips the degeneracy guard.
4. The result holds in **both** walk-forward directions.
5. Because `flow_source = proxy_eod`, even a full pass is **motivating only**;
   capital consideration additionally requires re-confirmation on `alerts_real`
   (not available in the current tier).

**Anything short of all five → fail to reject H0: no demonstrated flow edge.**
Reported plainly. No re-running with fresh thresholds to fish for a pass (that is
the peeking bug that faked the original ledger).

## 8. Frozen constants

```
FLOW_EXP_MATERIAL_MARGIN_USD = 15.0     # per-trade net edge at k=1.0 to call material
FLOW_EXP_EMBARGO_DAYS        = 55       # >= max DTE
FLOW_EXP_FILL_K              = 1.0      # verdict fill
CHAIN_ATM_BAND_PCT           = 0.12     # near-ATM strike band for chain aggregation
CHAIN_N_EXPIRIES             = 2        # front expiries aggregated
MIN_CHAIN_CONTRACTS          = 6        # min usable contracts for a flow read
BOOTSTRAP_ITERS              = 10000
GRID_COMBINATIONS            = 36
```

`MATERIAL_MARGIN_USD = 15` is this author's pre-registered choice (~⅔ of the
observed ~$24/trade spread tax). It is open to the auditor's revision **before the
verdict run** — changing it after seeing results is forbidden.

---

## Amendment 1 — pre-results (2026-07-22, on auditor guidance)

Two facts surfaced *after* the original freeze but *before* any results, disclosed
here per the deviation rule (a pre-results correction is legitimate; changing
anything after seeing results is not).

**A. Flow-data horizon.** The flow-side `/historic` fields (`ask_volume`,
`bid_volume`, `sweep_volume`, `multi_leg_volume`, `total_premium`) are densely
populated only from **~2023 onward** — for a 2021–22 contract they appear on ~2% of
days. Pricing/NBBO/IV reach 2021, but the **flow proxy is only buildable
~2023–2026.** Correction to §2's caveat: `multi_leg_volume` **is** present on this
window, so single-vs-multi-leg *is* captured; only the opening-vs-closing-trade
discrimination is missing.

**B. §4.2 honored by intent, not by relaxation.** The 2021 melt-up and the sustained
2022 bear are unreachable. Rather than lower the bar, §4.2's purpose (stop a regime
coincidence passing as edge) is met by promoting the **April-2025 ~19% risk-off
drawdown to the mandatory stress fold**:

- **Mandatory stress fold.** The walk-forward fold whose test window covers the
  spring-2025 selloff (`FLOW_EXP_STRESS_WINDOW = 2025-02-15 .. 2025-04-30`) is the
  stress fold. **The CONFIRM−OPPOSE lift MUST hold the same sign through it** — a
  flow edge that appears only in the up-tape and reverses in the drawdown fails
  (the same trend-follower-gets-chopped failure the structures already showed). This
  makes §4.4's same-sign requirement concrete and non-negotiable at the drawdown.
- **Regimes present and tested:** 2023 recovery, 2024 melt-up, chop, and a sharp
  ~19% drawdown (spring 2025).
- **The one gap, named precisely:** *not* tested against a sustained multi-quarter
  grinding bear (2022-style). The difference is a **fast crash vs. a slow bleed** —
  the stress fold covers severe fast downside, not the prolonged-grind character.

**Bounded conclusion (frozen).** A pass may be reported only as: *"flow adds edge
across uptrend, chop, and a sharp ~19% drawdown (2023–2026); sustained
grinding-bear behavior untested."* Never as full regime coverage.

**Sequencing (frozen).** The paid path (deeper historical flow to 2021–22) is gated
on the free result: money is spent to close the last regime gap **only if** the free
experiment shows a real CONFIRM−OPPOSE lift that survives the April-2025 stress fold.
A null here — including through the drawdown — is a sufficient answer to stop.

**Added frozen constant:** `FLOW_EXP_STRESS_WINDOW = 2025-02-15 .. 2025-04-30`.

---
*Research and decision-support only — not investment advice. This experiment
characterizes a signal's out-of-sample behavior on historical marks; it is not a
forecast and does not authorize any trade.*
