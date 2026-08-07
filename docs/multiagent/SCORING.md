# Scoring methodology

Every threshold on this page lives in
[`config/methodology.yaml`](../../config/methodology.yaml), version
`ma-methodology-2026.08-v1`, which is stamped on every stored score.

## What the number is, and is not

A **deterministic rubric score out of 100**, computed by application code from
measured data. No LLM contributes a point.

It is stamped **UNCALIBRATED**. No feature in this repository has cleared
out-of-sample validation ([PRODUCT_STANCE.md](../PRODUCT_STANCE.md)), so an 86
means "scores well against the rubric", not "86% likely to work". The rubric
encodes judgement about what a good setup looks like; whether that judgement
predicts anything is an empirical question the data has not yet answered.

## The eight categories

| Category | Weight | Measures |
|---|---:|---|
| Catalyst strength | 15 | Does the reason for the trade exist, is it fresh, is it timely, is it corroborated, is it already priced in? |
| Market / sector alignment | 10 | Does the direction agree with SPY, QQQ, the sector proxy? Relative strength. |
| Technical setup | 20 | Trend, moving averages, relative volume, momentum, room to the opposing level, ATR, extension. |
| Options flow | 15 | Net premium lean, at-ask share, sweeps, size vs open interest, strike concentration. |
| IV / Greeks structure | 10 | IV rank, term structure, long-leg delta, theta burden. |
| Contract liquidity | 10 | Bid/ask spread, open interest, session volume. |
| Risk / reward | 15 | Reward-to-risk, breakeven vs implied move, budget fit, invalidation stated. |
| Data agreement / quality | 5 | Cross-provider price agreement, quote freshness, input coverage. |
| **Total** | **100** | |

A test asserts each category's rule points sum to its weight, so a category can
never silently cap below its stated contribution.

## Abstention: the mechanic that matters most

> **A rule with no input abstains. Its points leave the denominator. It does not
> score zero.**

Scoring a missing input as zero makes "we could not measure this"
indistinguishable from "we measured this and it was bad" — and those lead to
opposite decisions. `CLAUDE.md` §4 traces 67 of 67 bad signals to one `or 0.0`.

```
technical_setup: 12/14 → 17.14/20 weighted      (coverage 70%)
    technical.trend_aligned:  +5/5   (measured 6.2% >= 0)
    technical.key_ma:         +4/4   (measured 3.1% >= 0)
    technical.relative_volume: +3/3  (measured 1.4x >= 1.3)
    technical.momentum:       ABSTAINED (NA_no_data) — 3 pts removed from denominator
    technical.room_to_target: ABSTAINED (NA_no_data) — 3 pts removed from denominator
    technical.atr_supports_move: +0/2 (measured 0.4% in [1, 8])
    technical.extended:       [penalty] not triggered (measured 1.2ATR, limit 2.5)
```

The composite renormalises over measured weight, so an abstained category does
not silently cap the score — and `input_coverage` is reported next to every
score so a 78 at 55% coverage is never presented as the same claim as a 78 at
100%. Below `hard_rules.min_input_coverage` (0.60) the candidate is
hard-rejected outright.

## Partial credit has a floor

Partial-credit rules scale between a **floor** (the value at which the rule
awards nothing) and the threshold — not from zero.

Scaling from zero means a measurement at its natural baseline collects most of
the points: a relative volume of exactly 1.0x, which is *dead average and by
definition no signal*, would score 1.0/1.3 = 77% of the credit for "volume is
strong". Floors in use: relative volume 1.0x, momentum agreement 0.50 (a coin
flip), flow concentration 0.25, room-to-target at the crowded distance.

## Penalties

Penalties carry `points_possible = 0`, so they can subtract without inflating
the denominator — otherwise adding a penalty would *raise* a category's ceiling.
A category is clamped at zero so a runaway penalty cannot steal points from
another category. A penalty whose condition cannot be evaluated does not fire.

| Penalty | Fires when |
|---|---|
| `catalyst.stale` | newest supporting item older than 5 days |
| `catalyst.priced_in` | underlying already moved >6% **with the thesis** since publication |
| `alignment.fighting_tape` | direction opposes both SPY and QQQ |
| `technical.crowded_level` | within 0.5 ATR of the opposing level |
| `technical.extended` | more than 2.5 ATR from the 20-bar SMA |
| `flow.contradiction` | net premium leans against the thesis |
| `iv.elevated` | IV rank above 0.70 |

Two details worth stating. The priced-in check only counts moves *in the thesis
direction* — a stock that fell 8% since bullish news has not priced it in. And
it is a heuristic, not a measurement of information absorption, which is why it
is a small penalty rather than a rejection.

## Tri-state booleans

"Aligned with SPY" is `bool | None`, not `bool`. A bullish candidate in a
**neutral** tape is neither aligned nor fighting it, and collapsing that to
`False` would let a flat market spend the fighting-the-tape penalty. `None`
abstains.

## How options flow is scored, and why lightly

The spec is explicit: *"The system must NOT assume that every large options
transaction is bullish or bearish."* Three concrete consequences:

1. **Premium, not contract count.** A thousand far-OTM lottery tickets and one
   institutional block are not comparable by size.
2. **Below `min_premium_usd` ($50k) the whole category abstains.** Reading thin
   flow manufactures confirmation out of noise.
3. **Direction is a conclusion.** `implied_bias` is set only when net premium
   leans past 0.60; otherwise `direction_ambiguous` stays true and the
   directional rule scores nothing. Prints where the vendor did not state a side
   are excluded from the at-ask denominator, never counted as bid-side.

The weight is 15 of 100 and the contradiction penalty (−5) is larger than any
single credit. That asymmetry is deliberate:
[FLOW_EXPERIMENT_DISPOSITION.md](../FLOW_EXPERIMENT_DISPOSITION.md) records a
pre-registered experiment on this repository's own data that **failed to reject
the null** on flow-as-confirmer, and found at-ask aggression to be if anything
*negatively* associated with outcome. Flow that contradicts you is more
informative than flow that agrees.

## Classification bands

| Score | Label | Name |
|---|---|---|
| 90–100 | `EXCEPTIONAL` | Exceptional |
| 80–89 | `HIGH_CONVICTION` | High conviction |
| 70–79 | `GOOD` | Good candidate |
| 60–69 | `WATCHLIST` | Watchlist / conditional |
| < 60 | `REJECT` | Reject |

Configurable in `classification.bands`. `calibration_status` travels alongside
and reads `UNCALIBRATED`.

## Hard rejection rules

Terminal. **They never see the score** — `evaluate_hard_rules()` reads
measurements and (for the coverage rule) `input_coverage`, never the composite.
A 95 and a 40 with identical measurements produce identical rejections.

| Code | Threshold |
|---|---|
| `SPREAD_TOO_WIDE` | worst leg spread > 15% of mid |
| `INSUFFICIENT_LIQUIDITY` | OI < 100 or volume < 10 on the thinnest leg |
| `MISSING_CRITICAL_DATA` | no two-sided market, no max loss, no timestamp |
| `CATALYST_UNVERIFIED` | no cited evidence resolves |
| `EARNINGS_BLACKOUT` | earnings within 2 days (waived if earnings *is* the thesis) |
| `PROVIDER_DISAGREEMENT` | independent price sources differ by > 2% |
| `REWARD_RISK_TOO_LOW` | below 1.2 |
| `COST_EXCEEDS_RISK_BUDGET` | one contract risks more than $100 ([RISK_POLICY.md](../RISK_POLICY.md)) |
| `EXCESSIVE_THETA` | decay over the hold > 60% of premium paid |
| `STRATEGY_NOT_ALLOWED` | outside the four permitted structures |
| `NO_VALID_CONTRACT` | nothing in the chain met the bands |
| `STALE_QUOTE` | option quotes older than 30 minutes |
| `INSUFFICIENT_COVERAGE` | less than 60% of the rubric measurable |

All rules run — the pipeline does not short-circuit on the first — so a report
can say a candidate failed on three counts rather than making you fix them one
at a time.

**Absence is not automatic failure.** A missing spread does not fail the spread
rule; it fails `MISSING_CRITICAL_DATA`, which is a different diagnosis with a
different fix. Two rules do treat absence as failure and say so: critical-data
presence and coverage.

`BELOW_MINIMUM_SCORE` is deliberately **not** a hard rule. "Did not score well
enough" and "is disqualified" are different findings, and the report separates
them — the first group is where the interesting post-hoc questions live.

## Auditing a score

Every `ScoreRule` records the measurement it saw, the threshold it compared
against, and whether it measured or abstained. The composite reconstructs
exactly from its leaves, and a test asserts it.

```bash
python run_market_scan.py                       # audit printed by default
curl localhost:8000/multiagent/candidates/<id>/audit
```

```
risk_reward: 11.4/15 -> 11.4/15 weighted
    rr.reward_to_risk:       +2.4/6 (measured 1.577 >= 2)
    rr.breakeven_reachable:  +4/4   (measured 0.3889 <= 0.8)
    rr.within_budget:        +3/3   (measured 0.97 <= 1)
    rr.invalidation_defined: +2/2   (measured 1 >= 1)
TOTAL: 65.041 of 100 measured weight -> 65.04/100 (coverage 96%, UNCALIBRATED)
```

## Changing the methodology

Edit `config/methodology.yaml`, bump `version`, and re-run. The version string
is stored on every score, so rows produced under different rubrics stay
separable and are never pooled by accident.

**Do not** add a threshold to Python.
`tests/multiagent/test_config_and_definitions.py` greps the scoring engine for
bare numeric literals in `threshold=`, `low=`, `high=` and `floor=` and fails if
one appears — a threshold in code is a threshold nobody can review.

## Not implemented, by design

**No AI self-modification of scoring rules.** The data is stored so that
statistical analysis can one day *recommend* methodology changes; applying one
is a human editing a reviewable file and bumping a version.
