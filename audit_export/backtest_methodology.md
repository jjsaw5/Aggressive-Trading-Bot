# Backtest & Signal Methodology Memo

Generated 2026-07-31 · export build `7afa098` · warehouse as of export time.
Answers the eight required questions. Where the system cannot answer, the answer
is "it cannot", not an estimate.

---

## 0. Two things that reframe everything below

**There are no "high conviction" signals.** Conviction is gated
(`app/shortduration/conviction_gate.py`) and the gate is **RED**: 3 of 5 criteria
fail. Every exported row carries `conviction_status=UNCALIBRATED`. What this
export contains is the highest-**scoring** signals. That is a different claim,
and the distinction is the point of the gate.

```
green: False
  [FAIL] validated_feature   registries empty — 7 pre-registered/OOS tests, all null
  [PASS] calibration_sample  n_decisive=136, grade=real_marks
  [PASS] brier               0.0849 (need <=0.25)
  [FAIL] discrimination      spearman=0.1753, 95% CI [-0.0114, 0.3477] includes zero
  [FAIL] per_regime          0 regimes with n>=10 (need >=2)
```

**There is no backtest behind these signals.** The exported rows are *live
forward decisions* recorded as they fired, graded afterwards. A separate
real-mark backtest corpus exists (`docs/CORPUS_REAL_MARK_2021_2026.md`) but it
did not produce these signals and its results are not mixed in here. Questions
below are answered for the forward record, and the distinction is flagged where
it changes the answer.

---

## 1. Data source and granularity

| Input | Vendor | Granularity | As-of signal time? |
|---|---|---|---|
| Underlying quote | FMP | real-time snapshot | **Yes** — freshness-gated, `quote_age_s` recorded per candidate |
| Intraday bars | FMP | 1-minute, RTH | **Yes** |
| Daily history (SMA/RSI) | FMP | daily closes | **Last completed session** — see §5 |
| Option chain | Unusual Whales | snapshot at scan | **Yes** |
| Options flow | Unusual Whales | alert stream | **Yes** |
| News | Benzinga | headline stream | **Yes** |
| Historical option marks (grading) | Unusual Whales | **daily NBBO mid** | Post-hoc |
| Live fills | Robinhood | actual executions | Actual |

**Option quotes are NBBO mids, not trade prints.** For grading, they are **daily**
bars — one mark per contract per day. Consequence: an intraday target touch is
invisible to the managed-policy grader, which can only see closes. Reported as a
gap in §2, not corrected.

## 2. Fill model

| | Entry | Exit | Slippage |
|---|---|---|---|
| Scanner signals (`source=scan`, n=67) | mid (`est_fill_net`) | mid | **Commission only.** No synthetic slippage — the mid-to-mid mark series is treated as already reflecting the crossing |
| Live trades (`source=live`, n=78) | actual fill | actual fill | n/a — real |

**For 0DTE this does not meet the spec's bar and I am not going to claim it
does.** The spec requires a documented dollars-per-contract slippage assumption
for 0DTE and rejects bare "mid". The system has none: `slippage_model` reads
`commission_only` on every 0DTE scanner row. The `pnl_at_1tick_worse` stress
column the spec requires is `NA_not_implemented` — the stored data has no bid/ask
to perturb.

What *is* measured is round-trip spread cost at signal time (`cost_drag_ratio` on
the live candidate) — but it was never copied onto the warehoused snapshot, so it
reads `NA_not_implemented` in this export. Observed live values run 2–36% of max
risk, which is large enough that ignoring it changes conclusions.

## 3. Sample sizes and date range

**Signal dates 2026-04-10 → 2026-07-31. Resolutions 2026-04-15 → 2026-07-31.**
~16 weeks.

| Bucket | Scanner signals (resolved) | Live human trades | Meets 30-row minimum? |
|---|---|---|---|
| 0DTE | **38** | 10 | Yes |
| 1-5DTE | **29** | 19 | **No — 29** |
| LONG | **0** | 49 | **No — zero scanner signals** |

The LONG bucket contains **no scanner signals at all**. Its 49 rows are
discretionary human trades that happen to be long-dated. Any LONG-bucket
statistic in `calibration_by_composite_score.csv` describes a human, not the
scanner.

## 4. Regime coverage

**Cannot be confirmed. This is a hard gap.**

The forward record is ~16 weeks of a single market period. The system does not
store a VIX series and does not tag signals by market regime in a way that
survives into the warehouse (`iv_rank` is null on **all 145** rows — see §8). So
I cannot list a VIX-spike window, a trending window, and a range-bound window
with dates, because the data to classify them was never captured.

This is also why the conviction gate's `per_regime` criterion fails: 0 regimes
with n≥10. The gate and this memo are reporting the same hole.

## 5. Look-ahead audit

Component by component, for the 8 scoring inputs:

| Component | Inputs | Computable at signal time? |
|---|---|---|
| `price_structure` | 1-min bars ≤ now, opening range | Yes |
| `relvol_momentum` | intraday volume vs profile | Yes |
| `market_alignment` | breadth across watchlist, current | Yes |
| `flow_quality` / `multi_session_flow` | UW alerts ≤ now | Yes |
| `contract_liquidity` | chain snapshot at scan | Yes |
| `volatility` | IV of traded expiry at scan | Yes |
| `catalyst_news` | headline stream ≤ now | Yes |
| `risk_reward` | structure economics at scan | Yes |
| `daily_trend` | SMA20/SMA50/RSI14 over daily closes | **Last completed session only** |

**No look-ahead found in scoring.** `daily_trend` deserves the flag it has:
its inputs are yesterday's closes, so on a large intraday move the trend read is
up to one session stale. Observed live: an INTC signal read "RSI 25 — weak
momentum" from the prior close while the stock was up 11.6% intraday. Stale, not
look-ahead — the value *was* computable at signal time — but it means the
strongest bearish input can describe a state that no longer exists.

**Two related defects found and fixed during this audit period**, both of which
could make the scanner blind rather than wrong:

- `build_context` requested "most recent available session" bars while levels
  were computed against `now`. On disagreement the opening range came back empty
  and ORB detected nothing — indistinguishable from a quiet market. Fixed
  (PR #47).
- Only `opening_range` filtered bars by date, so a multi-session response blended
  VWAP, last, and relative volume across days. Fixed (PR #47).

**A resolution-side limitation, not look-ahead:** managed-policy grading walks
*daily* marks, so a target hit intraday and given back by the close is never
seen. It biases measured performance **downward**, not upward.

## 6. Survivorship

**Not handled.** The universe is a static configured watchlist
(`app/engine/universe.py`), not a point-in-time index membership. No delisting,
halt, or ticker-change logic exists. For a 16-week window over large-cap liquid
names the practical exposure is small, but it is unhandled rather than
controlled.

Expired-worthless contracts *are* handled correctly: at expiration a defined-risk
structure has no extrinsic value, so signed intrinsic is the exact payoff
(`app/analytics/expiry_settlement.py`).

## 7. Selection integrity

**This is the weakest area and I want it stated plainly.**

- **Scoring weights were revised during the live period.** The 0DTE weight vector
  was rebalanced and versioned mid-stream (`sd-scoring-2026.07-v3`). Signals
  before and after carry different `scoring_model_version` values.
- **There is no held-out period.** No holdout window was reserved, so none can be
  quoted. Any statistic here is in-sample with respect to weight selection.
- **What protects against silent fitting:** seven features went through
  pre-registered out-of-sample validation with the hypothesis committed to the
  repo *before* results (`docs/*PREREGISTRATION*.md`). **All seven came back
  null, and none was promoted into scoring.** The feature registry is empty,
  which is why `validated_feature` fails.

So: nothing has been fitted *and then claimed as validated*. But the composite
weights themselves are hand-set and un-validated, and the exported statistics
cannot be treated as out-of-sample.

## 8. Pre-fix vs post-fix comparison

Two mid-stream fixes are in scope. Only one supports a same-signal-set
comparison.

### Grading policy: hold-to-expiry vs managed exit — **same 59 decisions**

| Grade | n | Win rate | Mean R | Total |
|---|---|---|---|---|
| Hold-to-expiry (pre) | 59 | 22.0% | −0.298 | −$1,033 |
| Managed exit (post) | 59 | **23.7%** | **−0.242** | **−$839** |

Grading under the policy actually run improves the measured result by ~0.06R per
trade. It does not change the sign. Score↔P&L discrimination moved more sharply
— Spearman 0.026 → 0.175 — but the bootstrap CI still includes zero.

### IV-rank wiring — **comparison not possible**

`iv_rank` is null on **all 145 exported rows**. The fix captures it going
forward; it cannot be backfilled, because the value is a point-in-time reading
that was never stored. There is no pre/post split to compute. This is a gap, and
it is the direct cause of the `per_regime` gate failure and of §4 above.

---

## What the numbers say, restricted to scanner signals

Live human trades are excluded here — mixing them in was the first thing that
made the raw calibration table look like an inverted score.

**Scanner signals only (n=67), by composite-score decile:**

| Score bin | n | Win rate | Mean R |
|---|---|---|---|
| 30-40 | 1 | 0% | −1.065 |
| 40-50 | 2 | 0% | −1.096 |
| 50-60 | 12 | 17% | −0.541 |
| 60-70 | 19 | 26% | −0.345 |
| **70-80** | **31** | **13%** | **−0.656** |
| 80-90 | 2 | 100% | +5.844 |

The modal bin (70-80, n=31) has a **worse** win rate and worse mean R than 60-70.
The 80-90 bin is n=2 and carries no information. There is no monotone
relationship between score and outcome in this sample — consistent with the
gate's own discrimination measurement (CI includes zero).

| Bucket | n | Win rate | Mean R |
|---|---|---|---|
| 0DTE | 38 | 23.7% | −0.143 |
| 1-5DTE | 29 | 13.8% | −0.674 |

**Both buckets are negative-expectancy over this sample.**

## 0DTE-specific findings the spec asks to surface

- **`session_segment_score`: not implemented.** The 0DTE and 1-5DTE models use
  *different weight vectors and different components* (0DTE: price structure,
  relvol, flow, liquidity; 1-5DTE: daily trend, news, multi-session flow), so the
  logic is genuinely distinct — but neither model varies its weights by time of
  day. `time_of_day_bucket` is exported so the question is answerable from the
  CSV; the scorer does not use it.
- **`gex_proxy`: not implemented.** No dealer-positioning input of any kind.
- **`pnl_at_1tick_worse`: not implemented.** No stored bid/ask to perturb.

## 1-5DTE

`earnings_days_away` is **`NA_not_implemented` on every row** — the guardrail
computes it live and writes it into the thesis text, but it was never persisted
to the snapshot. So the export cannot confirm earnings filtering from data.

Stated from code: earnings plays are **included, with a warning, not excluded**.
`_structural_warnings` emits "Earnings … land before expiry — this is an event
binary" but it is advisory text; it is not a gate and does not affect the score
or the pick list. An AAPL call spread was picked #1 the day before earnings on
this basis.

## Longer-dated

`theta_at_entry` and `vega_at_entry` are **not implemented** — Greeks are
available on the chain at scan time but never copied to the snapshot. The spec's
check (is the score rewarding a carry profile that contradicts the thesis?)
**cannot be performed** from this export.
