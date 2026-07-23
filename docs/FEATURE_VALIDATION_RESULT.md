# Feature-Validation Result — no feature earned conviction (honest null)

Conviction-Scanner spec §3/§4. The harness asks, for each candidate entry-time
feature, one falsifiable question: **does it predict a net-of-cost outcome
out-of-sample?** Method: walk-forward folds (purge + embargo), predictor fit on
train only (numeric features oriented by train sign; categoricals target-encoded
on train means), scored on held-out test folds; forward walk gives the bootstrap
CI, reverse walk is an independent same-sign check; Bonferroni-corrected over the
7 features; broken down per vol regime (a sign that flips across regimes is
conditional beta, not edge).

Run against the engine-selected corpus (2021→2026, 5 split-free names, every leg
repriced from recorded UW NBBO, label = net-of-cost win at k=1.0 / full spread).
**n = 218 trades, 159 out-of-sample pairs.**

## Verdict: nothing validated. Registry is empty.

| feature | fwd OOS ρ | Bonferroni CI | reverse ρ | regime flip | validated |
|---|---:|---|---:|:--:|:--:|
| iv_level | +0.129 | [−0.090, +0.345] | +0.086 | no | ❌ |
| direction | +0.098 | [−0.115, +0.302] | +0.147 | **yes** | ❌ |
| structure | +0.074 | [−0.135, +0.294] | +0.152 | **yes** | ❌ |
| spot_momentum | +0.055 | [−0.150, +0.249] | −0.021 | **yes** | ❌ |
| entry_spread_pct | −0.026 | [−0.256, +0.206] | −0.010 | no | ❌ |
| iv_rank | −0.069 | [−0.292, +0.156] | +0.060 | **yes** | ❌ |
| dte | −0.071 | [−0.283, +0.135] | −0.097 | no | ❌ |

**Every feature's out-of-sample CI straddles zero.** Not one clears the
Bonferroni-corrected bar. The features with the largest positive point estimates —
`direction`, `structure`, `spot_momentum` — are exactly the ones whose sign
**flips across vol regimes**: the conditional-beta signature the corpus already
showed at the aggregate level, now confirmed feature-by-feature.

## What this means

1. **No conviction can be earned from this corpus.** The registry
   (`docs/feature_registry.json`) is empty by design — nothing survived. This is
   not a harness failure; it is the harness working. It refuses to hand weight to a
   feature that hasn't proven itself out-of-sample, which is the entire point of
   "conviction earned, never asserted."
2. **Layer-1's UNCALIBRATED degrade stays in force**, now on rigorous footing. The
   number the app shows is a tradability rank; there is no validated feature behind
   it, so it must never present as conviction.
3. **Layer-2 (calibrated conviction, #77) has nothing to calibrate on yet.** A
   calibration gate needs at least one validated signal to gate; with an empty
   registry, the correct state is permanently UNCALIBRATED. Building the gate is
   still worthwhile as the *mechanism* that would light up if a future feature
   validates — but it will (correctly) stay red on today's data.

## Construct note — `iv_rank` is a proxy, causal but not the live feature

The `iv_rank` feature tested here is the **trailing percentile of each contract's own
recorded IV over its short life** (`_iv_rank_proxy`). It is **causal** — computed only
from bars on/before the entry date, no look-ahead (verified). But it is **not** the
construct the live scanner consumes (the underlying's 1-year IV rank from UW). So the
null on `iv_rank` is a verdict on the trailing-contract-IV-percentile proxy, not on the
live underlying IV rank; re-test on the live construct before generalizing. (The other
features — dte, iv_level, entry_spread_pct, spot_momentum, direction, structure — match
their live constructs.)

## Honest limits (why this is "not yet," not "never")

- **Power.** n_oos = 159 with Bonferroni over 7 features is a demanding bar; a
  small-but-real effect could hide under it. The point estimates are near zero,
  though — this reads as absence of a strong edge, not merely low power.
- **Feature set is price/vol/structure/cost only.** The UW tier has no historic
  flow-alerts pre-2023 and the corpus carries no flow, so the flow features that
  are the app's premise were **not tested here**. Testing them needs a flow-bearing
  corpus (2023+), which is the parked §8 work. That is the most likely place a real
  feature could still be hiding — and the honest next question.
- **Universe is 5 split-free names.** Widening (split-adjusted) would add power and
  breadth.

## Reproduce

```
UW_HISTORIC_ENABLED=true UNUSUAL_WHALES_API_KEY=… \
    python -m scripts.validate_features --preset all
```

Writes `docs/feature_registry.json`. Deterministic (seeded bootstrap).
