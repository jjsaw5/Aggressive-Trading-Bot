# Signals Data Dictionary

Covers every column in `signals_export.csv`, `forward_log.csv`, and
`calibration_by_composite_score.csv`.

**Timezone:** all timestamps are **America/New_York (ET)** with UTC offset,
ISO 8601. Source values are stored UTC and converted on export.

**Missing-value sentinels** — never blank, never zero:

| Sentinel | Meaning |
|---|---|
| `NA_not_implemented` | The system has no such concept. No amount of reprocessing produces it. |
| `NA_no_data` | The concept exists, but this row has no value for it. |
| `NA_unresolved` | Outcome not yet determined. |

**Row scope:** one row per resolved decision carrying a dollar P&L. Two
populations, distinguished by `signal_source`:

- `scan` (n=67) — scanner-generated signals with a full score breakdown.
- `live` (n=78) — discretionary human trades synced from Robinhood, warehoused
  as decisions so they can be graded on the same footing. **These have no
  scanner score**; `composite_score` is 0 and all `component_*` fields are
  `NA_no_data`. Do not read them as scanner performance.

---

## 1A · Signal identification

| Column | Definition | Source | Formula |
|---|---|---|---|
| `signal_id` | Stable unique ID | `DecisionSnapshot.decision_id` | `sd:{candidate_id}` or `live:{trade_id}` |
| `signal_ts` | When the signal fired | `DecisionSnapshot.generated_at` | Set at detection, not at log write |
| `dte_bucket` | `0DTE` / `1-5DTE` / `LONG` | derived | `dte_actual<=0` → 0DTE; `<=5` → 1-5DTE; else LONG |
| `underlying` | Ticker | `DecisionSnapshot.symbol` | — |
| `strategy_type` | Structure archetype | `DecisionSnapshot.strategy` | `StrategyType` enum |
| `contract_details` | All legs | `TradePlan.legs` | `{B\|S}{qty}x{strike}{C\|P}@{expiry}`, ` / `-joined |
| `dte_actual` | Calendar days to expiry at signal | `DecisionSnapshot.dte_at_entry` | `min(leg.expiration) − detected_at.date()` |
| `scanner_version` | Scoring model lineage | `scoring_model_version` | e.g. `sd-scoring-2026.07-v3`. **`NA_no_data` on live rows** |
| `export_git_sha` | Build that produced **this export** | `git rev-parse` | Not the build that produced the signal — that was never recorded. **Gap.** |
| `signal_source` | `scan` or `live` | `DecisionSnapshot.source` | — |
| `conviction_status` | Always `UNCALIBRATED` | `conviction_gate` | The gate is red; no row is high-conviction |

## 1B · Score breakdown

| Column | Definition | Source | Formula |
|---|---|---|---|
| `composite_score` | Final score as displayed, 0–1 | `composite_score` | `ScoreCard.normalized` = Σ(raw×weight)/Σ(weight) |
| `predicted_pop` | Probability-of-profit claim | `probability_of_profit` | **Present on 29/145 rows only** |
| `predicted_pop_source` | POP construct | `pop_source` | `bs_zero_drift_traded_expiry_iv` = Black-Scholes zero-drift N(d₂) at the traded expiry's IV vs the structure break-even. Empty on legacy rows |
| `weights_sum` | Documented weight total | `ScoreCard.weights` | **Sums to 100.0** for both models |
| `component_N_name` | Scoring input key | `ScoreCard.factors[N].key` | — |
| `component_N_raw` | Raw value at signal time, 0–1 | `factors[N].raw` | — |
| `component_N_weight` | Weight in the composite | `factors[N].weight` | — |
| `component_N_direction` | Monotonicity | constant | Always `higher_raw_increases_score` — see below |

### Weight vectors (both sum to 100)

**0DTE** — `price_structure` 22, `contract_liquidity` 18, `market_alignment` 15,
`relvol_momentum` 15, `flow_quality` 10, `volatility` 10, `catalyst_news` 5,
`risk_reward` 5.

**1-5DTE** — `daily_trend` 20, `catalyst_news` 15, `multi_session_flow` 15,
`market_alignment` 10, `volatility` 10, `contract_liquidity` 10,
`technical_entry` 10, `risk_reward` 10.

### Direction

Every component is constructed so a **higher raw value is more favourable**;
`points = raw × weight` with no sign inversions anywhere in
`app/shortduration/scoring/components.py`. Direction is a property of the model,
not of the row, but it is emitted per row so the inverted-scoring failure mode
stays checkable from the CSV alone.

### Correlated component pairs — **flagged as the spec requires**

| Pair | Shared input | Note |
|---|---|---|
| `daily_trend` ↔ `technical_entry` | Same daily close series | SMA/RSI and entry quality both derive from `ctx.daily`; combined weight 30/100 on the 1-5DTE model |
| `flow_quality` ↔ `multi_session_flow` | Same UW alert stream | Different windows over one feed |
| `contract_liquidity` ↔ `risk_reward` | Same chain snapshot | `risk_reward` reads structure economics priced off the same quotes liquidity scores |
| `price_structure` ↔ `relvol_momentum` | Same 1-min bar series | Combined weight 37/100 on the 0DTE model |

These are **not** independent inputs. The composite treats them as if they were.

## 1C · Market context at signal time

**This section is mostly a gap.** The warehouse froze the *prediction*, not the
order book. A point-in-time quote cannot be reconstructed after the fact.

| Column | Status | Note |
|---|---|---|
| `spot_price` | **Available** | `entry_spot`, frozen at plan time |
| `option_bid` / `option_ask` / `option_mark` / `option_mid` | `NA_not_implemented` | Never persisted. The chain snapshot existed at scan; only the derived net was kept |
| `spread_pct` | `NA_not_implemented` | Requires bid/ask |
| `cost_drag_ratio` | `NA_not_implemented` | **Computed live** on the candidate (round-trip spread ÷ max risk) but never copied to the snapshot. Observed live values 2–36% |
| `iv` | `NA_no_data` on all 145 | `entry_iv` field exists; null on every historical row |
| `iv_rank_252d` | `NA_no_data` on all 145 | See below |
| `implied_move_to_expiry` | `NA_not_implemented` | No straddle-implied move is computed anywhere |
| `realized_vol_20d` | `NA_not_implemented` | HV is computed inside the mock provider only, never for live scoring |
| `vrp` | `NA_not_implemented` | No VRP on the signal path. A separate VRP *study* exists (`docs/VRP_STAGE*`) and returned null |
| `term_slope` | `NA_not_implemented` | `IVContext.term_structure_slope` exists on the provider but is not persisted |
| `volume` / `open_interest` | `NA_not_implemented` | Gate contract selection at scan; not stored |
| `earnings_days_away` | `NA_not_implemented` | Computed live for the guardrail, written into thesis prose, never persisted as a number |
| `time_of_day_bucket` | **Available** | Derived from `signal_ts` in ET: `open` <10:00, `morning` <12:00, `midday` <15:00, `power_hour` <15:45, `close` ≥15:45, else `outside_rth` |

### `iv_rank_252d` — named input series, as the spec requires

The intended source is `IVContext.iv_rank` from the Unusual Whales IV-history
endpoint: **rank of current 30-day constant-maturity IV within its own trailing
252-session range** — *not* the traded contract's IV, and not an index.

It is `NA_no_data` on **all 145 rows**. Short-duration candidates carried an
empty `signals` list and the extractor served only the funnel lineage, so the
value was dropped before the snapshot. Fixed for signals written after
2026-07-29; **not backfillable**, because it is a point-in-time reading.

## 1D · Entry and exit assumptions

| Column | Definition | Values |
|---|---|---|
| `entry_price_basis` | What the system assumed it paid | `actual_fill` (live) / `modeled_mid` (scan) |
| `entry_price` | Net per share, signed: debit>0, credit<0 | `entry_net_per_share` |
| `slippage_model` | — | `none_real_fill`, or `commission_only; mid-to-mid marks, no synthetic slippage added` |
| `profit_definition` | What "profit" means for this row | `realized_close` (live) / `managed_exit_first_trigger` (scanner) |
| `exit_rule` | Exact logic applied | Managed: target +X% of debit OR stop −Y% of debit OR time stop at N DTE, first trigger; **stop evaluated before target** so a bar through both books the loss |

**Basis consistency:** entry and exit basis are identical within every row by
construction — `actual_fill`/`actual_fill` or `modeled_mid`/`modeled_mid`. No row
mixes bases.

## 1E · Resolved outcome

| Column | Definition | Formula / status |
|---|---|---|
| `outcome` | `win` / `loss` / `scratch` / `unresolved` | Scratch band = ±5% of max risk |
| `exit_ts` | Resolution timestamp | `resolved_at`, ET |
| `exit_price_basis` | Matches entry basis | See above |
| `exit_price` | `NA_no_data` | The exit *net* was not persisted separately from P&L |
| `exit_reason` | `profit_target` / `stop_loss` / `time_stop` / `expiry` | Empty for grades that simulated no path |
| `pnl_usd_net` | Net of costs | `realized_pnl_usd` |
| `pnl_usd_gross` | Before costs | `realized_pnl_gross_usd` |
| `costs_usd` | Commission (+ modelled slippage where applied) | `costs_usd` |
| `pnl_pct` | Return on defined risk | `pnl_usd_net / abs(max_loss_usd)` |
| `r_multiple` | P&L ÷ defined risk | Same as `pnl_pct` — for a defined-risk debit the denominators coincide. Emitted separately rather than silently merged |
| `mfe` / `mae` | `NA_not_implemented` | Excursions are never tracked. Grading walks daily closes only |
| `hold_minutes` | Wall-clock signal→resolution | Computed. **Caveat:** for managed grades the resolution timestamp is when the *grader ran*, not when the exit triggered, so this overstates hold time on scanner rows |
| `elapsed_days` | Entry date → exit date | `elapsed_days`; this is the trustworthy duration field |
| `outcome_source` | Grading method | `live_close` (real fill) / `managed_policy` (daily-mark replay) |

## Bucket-specific columns

| Column | Bucket | Status |
|---|---|---|
| `session_segment_score` | 0DTE | `NA_not_implemented` — no time-of-day weighting exists |
| `gex_proxy` | 0DTE | `NA_not_implemented` — no dealer-positioning input |
| `pnl_at_1tick_worse` | 0DTE | `NA_not_implemented` — no stored bid/ask to perturb |
| `theta_at_entry` / `vega_at_entry` | LONG | `NA_not_implemented` — Greeks available at scan, never persisted |

## forward_log.csv

Same schema as `signals_export.csv`, restricted to `signal_source=live`
(n=78 real Robinhood fills), plus:

| Column | Definition |
|---|---|
| `execution_mode` | `live` — no paper rows resolved in this window |
| `actual_fill_price` | Real fill net per share |
| `backtest_expected_pnl` | `NA_not_implemented` — a live trade cannot be re-scored: it has no scorecard, and no point-in-time chain was stored to re-price against. **Backtest-vs-forward divergence therefore cannot be measured.** |

## calibration_by_composite_score.csv

**Filename is deliberate.** `predicted_pop` is present on only 29/145 rows, so
per the spec the fallback applies: bins are deciles of `composite_score`, not of
predicted probability.

| Column | Definition |
|---|---|
| `dte_bucket` | As above |
| `pop_bin` | Decile of `composite_score`, e.g. `70-80` |
| `n_signals` | Rows in bin |
| `n_decisive` | Wins + losses (scratches excluded) |
| `predicted_pop_mean` | Mean `composite_score` in bin |
| `actual_win_rate` | `wins / n_decisive` |
| `avg_win_r` / `avg_loss_r` | Mean R of winners / losers |
| `expectancy_r` | `win_rate×avg_win_r + (1−win_rate)×avg_loss_r` |
| `sample_sufficient` | `yes` if `n_decisive ≥ 20`, else `NO_insufficient_sample` |

**Read with care:** the `0-10` bins are dominated by live human trades, which
carry no scanner score. The scanner-only breakdown is in
`backtest_methodology.md`, and it is the one to use for judging the scanner.
