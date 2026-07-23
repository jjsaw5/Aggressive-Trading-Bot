# flow_sweep Power Pass — Pre-Registration

**Committed BEFORE any number was computed.** This is the ONE pre-registered
higher-power pass the auditor authorized for the single surviving thread from the
flow-feature validation (`FLOW_FEATURE_VALIDATION_RESULT.md`): `flow_sweep` at
+0.136 forward / +0.146 reverse, no regime flip, failing the corrected bar
(CI [−0.05, +0.32], n=236 OOS).

## Motivation and mechanism (why this feature earned one more look)

`flow_at_ask` (chasing) tilted negative while `flow_sweep` was sign-consistent
both walk directions with no regime flip. The mechanism argument: at-ask is
*direction* (who's paying up), sweep is *urgency under time pressure* (someone
crossing multiple books at once). Those are different behaviors, and it is
coherent for one to carry information while the other doesn't. A near-miss with a
plausible mechanism and sign-stability merits one more look; a bare statistical
near-miss would not.

## The hard stopping rule (binding)

**One pass.** Every encoding below that fails the bar is written to the registry
as **REJECTED and stays there**. No re-runs with fresh thresholds, no new
encodings after seeing results, no second EOD pass. A validated encoding becomes
a registry candidate (data-derived weight) and justifies evaluating an intraday
flow source; it does NOT enter the live score before clearing the registry gate.

## Power source (no new data, no API spend)

Same 25-name, 2023-2025 flow-bearing cache; same engine-selected vertical builder
as the original finding. Power comes from **densified entry sampling**: entry
offsets every 5 trading days from 25 to 90 (14 per (name, expiry)) instead of the
original (60, 40, 25). Expected n ≈ 4× the original (~1,200+ vectors).

**Dependence consequence, handled up front:** weekly entries with ~21-trading-day
holds overlap heavily, so observation-level inference would be anticonservative.
All CIs in this pass are **cluster bootstrap over (symbol, entry-month) clusters**
(seeded, 5,000 iters), replacing the i.i.d. pair bootstrap used at n=236.

## The FROZEN encoding family (m = 5 — Bonferroni α = 0.05/5 = 0.01 each)

All causal: computed from the bought (long-leg) option's bars in a trailing
window ending at the entry date. No additions, deletions, or re-parameterizations
after this commit.

1. **`flow_sweep`** — replication cell, unchanged: mean over trailing 5 days of
   `sweep_volume / volume`.
2. **`flow_sweep_x_at_ask`** — urgency × direction: trailing-5d mean of
   `(sweep_volume / volume) × (ask_volume / (ask_volume + bid_volume))`.
3. **`flow_sweep_oi`** — sweeps normalized by position base (EOD proxy for
   "opening" pressure): trailing-5d mean `sweep_volume` ÷ trailing-5d mean
   `open_interest`.
4. **`flow_sweep_burst`** — unusualness: entry-day `sweep_volume / volume` ÷
   trailing-20d mean of the same ratio (min 10 prior observations).
5. **`flow_sweep_persist`** — persistence: fraction of the trailing 5 days with
   `sweep_volume / volume > 0`.

## Inference (frozen — identical harness discipline, cluster-robust)

Walk-forward folds with purge + embargo (n_folds=4, embargo=7d), predictor
oriented by TRAIN-fold sign only, scored on held-out TEST folds. Forward walk
pools the OOS pairs and gives the **cluster-bootstrap CI at α=0.01**; reverse
walk is an independent same-sign check; per-vol-regime sign-flip guard (as
before). Label: net-of-cost P&L at k=1.0. Seed 12345.

**An encoding VALIDATES iff:** forward cluster-CI lower bound > 0 at α=0.01, AND
reverse-walk ρ ≥ 0, AND no per-regime sign flip. Anything else → REJECTED,
permanently.

## Granularity caveat (recorded so neither outcome is over-read)

A sweep is an intraday event; these are EOD aggregates — a degraded proxy.
- A **null** here is weak evidence against a real intraday sweep signal; it
  decides only that the EOD-visible version carries no harvestable information,
  and it makes paying for intraday data unjustified for now ("not worth paying
  for yet", not "disproven").
- A **positive** could partly be an aggregation artifact and is a *candidate*
  only — it justifies the intraday-data evaluation, not a score change.

## Deliverables

1. This pre-registration (committed first).
2. One run: per-encoding verdict table (forward ρ, cluster-CI, reverse ρ,
   per-regime signs, n), committed with the registry update.
3. Registry entries for all 5 encodings — validated or rejected — final.

*Research and decision-support only — not investment advice.*
