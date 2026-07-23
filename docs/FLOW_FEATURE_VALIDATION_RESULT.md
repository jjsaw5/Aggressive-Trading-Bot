# Flow-Feature Validation — the app's premise does not validate (honest null)

The price/vol/structure/cost features didn't predict net-of-cost outcomes
(`FEATURE_VALIDATION_RESULT.md`). But **flow is the app's actual thesis** — unusual
options activity: buyers lifting the offer, sweeps, big premium, unusual volume,
OI building. This is the test of that thesis.

Corpus: the flow-bearing cache from the flow experiment — **25 liquid names,
2023–2025**, real UW flow fields (`ask_volume`, `bid_volume`, `sweep_volume`,
`total_premium`). Engine-selected verticals repriced from recorded NBBO; five flow
features measured **on the option being bought**, causally, over a trailing window
ending at entry. Run through the **same** leak-safe harness (walk-forward, train-only
fits, forward-CI + reverse-sign robustness, Bonferroni over all 12 features,
per-regime sign-flip guard). **n = 305 trades, 236 out-of-sample, all with flow.**

## Verdict: nothing validated. Registry empty. Flow included.

| feature | fwd OOS ρ | Bonferroni CI | reverse ρ | regime flip | validated |
|---|---:|---|---:|:--:|:--:|
| **flow_sweep** | **+0.136** | [−0.051, +0.323] | **+0.146** | no | ❌ |
| flow_at_ask | −0.119 | [−0.298, +0.069] | −0.069 | yes | ❌ |
| flow_premium | −0.145 | [−0.332, +0.052] | +0.114 | yes | ❌ |
| flow_rel_volume | −0.078 | [−0.265, +0.115] | +0.039 | yes | ❌ |
| flow_oi_trend | −0.002 | [−0.197, +0.187] | −0.132 | yes | ❌ |
| iv_level | +0.129 | [−0.062, +0.315] | +0.118 | no | ❌ |
| dte | +0.124 | [−0.064, +0.291] | +0.056 | yes | ❌ |
| spot_momentum | +0.112 | [−0.063, +0.269] | +0.126 | no | ❌ |
| entry_spread_pct | +0.052 | [−0.137, +0.247] | +0.053 | no | ❌ |
| direction | +0.053 | [−0.130, +0.231] | −0.112 | yes | ❌ |
| iv_rank | −0.072 | [−0.264, +0.119] | +0.100 | yes | ❌ |
| structure | −0.005 | [−0.194, +0.173] | −0.221 | yes | ❌ |

**Every feature's Bonferroni CI straddles zero.** No flow feature earns weight. The
app's premise does not survive out-of-sample, net-of-cost testing at n=236.

## Two findings that matter more than the null

1. **`flow_at_ask` — the core "smart-money is buying" signal — is negatively
   tilted (−0.12).** Buying the option that others are aggressively lifting at the
   offer slightly *hurts* net-of-cost. That's the signature of **chasing**: by the
   time the at-ask print shows, you're paying up, and the spread tax finishes the
   job. Not significant, but the sign is the opposite of the thesis.

2. **`flow_sweep` is the one thread left.** Sweep intensity on the bought option is
   the only feature with a **sign-consistent positive** read across both walk
   directions (+0.136 forward, +0.146 reverse) and **no regime flip**. It still
   fails the Bonferroni-corrected bar (CI [−0.05, +0.32] includes zero), so it is
   NOT validated — but it is the single most promising signal in the whole study
   and the one place another pass could still turn something up.

## What this settles

- **No conviction can be earned from flow on this corpus.** Both registries are
  empty. Layer-1's UNCALIBRATED degrade stands; Layer-2 has nothing to calibrate on.
- **The evidence now converges from three independent directions** — the full-cycle
  real-mark backtest, the pre-registered flow experiment, and two feature-validation
  passes (pricing and flow). All say the same thing: **directional beta + spread
  tax, no demonstrable net-of-cost edge.**

## Honest limits (why "not proven," not "impossible")

- **Power.** n=236 OOS with Bonferroni over 12 features is demanding; `flow_sweep`'s
  effect (~0.14) is exactly the size that a larger corpus could confirm or kill.
- **EOD granularity.** These flow features are daily aggregates. Intraday flow (the
  timing of a sweep within the session) is invisible here and is the app's real
  live signal — untestable on this feed.
- **Feature engineering.** Five reasonable flow constructions ≠ all of them. A
  smarter encoding (e.g. sweep × at-ask × opening-position, direction-aware) could
  behave differently. `flow_sweep` says that's where to look.

## Reproduce

```
SCRATCH=/path python -m scripts.validate_flow_features   # reads the flow_experiment cache
```

Writes `docs/flow_feature_registry.json`. Deterministic (seeded bootstrap).
