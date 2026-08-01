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

**Two capture eras — read every §1C/§1E column against the right one:**

| Era | Boundary | What §1C / mark-quality columns hold |
|---|---|---|
| **Pre-capture** | signals before Phase 1 deploy | `NA_no_data`. Market context was computed on every scan and discarded before the warehouse. **Not backfillable** — a point-in-time quote cannot be reconstructed. |
| **Capture** | signals after Phase 1 deploy | Populated. |

`NA_no_data` in §1C therefore means "this row predates capture", NOT "the system
cannot do this". `NA_not_implemented` continues to mean the latter.

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

Frozen onto the decision at scan time (`MarketContext`) — see the two-era note
above. Everything here is **recorded, never scored**:
`CAPTURE_WINDOW_PREREGISTRATION.md` §2 permits persistence precisely because it
does not change what the scorer computes, and `tests/test_scoring_freeze.py`
enforces that no scoring module can import it.

### Structure pricing — NBBO

The flat columns describe the **primary long leg**. A debit structure's identity
sits in the leg it is long; the short leg is a financing choice. Every leg's full
quote ships in `legs_nbbo_json`, because item 1.1 is NBBO *per leg* and
collapsing a spread to one book loses the thing being measured.

| Column | Definition | Source | Formula |
|---|---|---|---|
| `spot_price` | Underlying at plan time | `entry_spot` | Frozen at scan. **Never 0.0** — see B1 |
| `option_bid` / `option_ask` | Primary long leg NBBO | UW `option-contracts` | `nbbo_bid` / `nbbo_ask` |
| `option_mid` | Midpoint | derived | `(bid+ask)/2`, **only** when `ask >= bid`. A crossed book yields `NA_no_data`, not a midpoint |
| `option_mark` | — | — | `NA_not_implemented` **by design.** No provider in the stack publishes a consolidated mark. Per Ruling 2 the spec is amended: where no vendor mark exists, `option_mid` off a real two-sided book is the reference price and this stays NA. Inventing one would be worse than declaring its absence |
| `spread_pct` | Relative spread | derived | `(ask-bid)/mid`; `NA_no_data` when `mid <= 0` |
| `legs_nbbo_json` | Every leg, full quote | derived | JSON array: strike, type, expiration, signed qty, bid/ask/mid/spread, volume, OI, iv, delta/gamma/theta/vega, `greeks_source`, `quote_source` |

### Cost

| Column | Definition | Source | Formula |
|---|---|---|---|
| `cost_drag_ratio` | Round-trip spread tax as a share of defined risk | computed at scan | `(Σ half-spread × 2 × 100 × contracts) / max_loss_usd`. Requires **every** leg two-sided; a partial sum would understate the tax |
| `round_trip_cost_usd` | Same tax in dollars | derived | The ratio's numerator, emitted so its denominator is auditable |

### Volatility

| Column | Definition | Source | Formula |
|---|---|---|---|
| `iv` | 30-day ATM IV | UW `volatility/stats` | `iv` field |
| `iv_rank_252d` | Rank of current IV30 in its trailing 252-session range | UW IV history | See the note below |
| `iv_percentile` | Percentile of the same series | UW IV history | — |
| `iv_rank_source` | How rank was derived | — | `iv_history` (true rank) / `hv_proxy` (realized-vol stand-in — **a different construct**, and the scorer discounts it 0.85) / `provider` |
| `term_slope` | Front-to-back ATM IV slope | UW `volatility/term-structure` | `iv(back ~30DTE) − iv(front, smallest DTE ≥ 1)`. **Negative = backwardation** (IV-crush risk for a debit buyer). DTE 0 is excluded from the front leg: the expiration-day ATM solve is unstable (SPY: 0.24 at dte=0 vs 0.08 at dte=3) and anchoring there would manufacture backwardation on every 0DTE-listed name. **Populated only from `sd-scoring-2026.08-v3.1`** — see FINDING_01 |
| `iv_skew` | OTM-put IV minus OTM-call IV | derived from chain | `>0` = downside fear |
| `implied_move_to_expiry` | Expected move as a fraction of spot | derived | `iv_traded_expiry × sqrt(dte/365)`. Uses the **traded** expiry's IV, not IV30 — a 30-day IV over a 2-day horizon badly overstates the move |
| `implied_move_usd` | Same, in dollars | derived | `implied_move_to_expiry × spot` |
| `realized_vol_20d` | 20-day realized vol on the underlying | `IVContext.hv20` | Annualised stdev of 20 daily log returns |
| `vrp` | Variance risk premium, **in vol points** | derived | `iv30 − hv20` |
| `vrp_ratio` | Same, dimensionless | derived | `iv30 / hv20` |

Both VRP conventions ship named, because "VRP" is ambiguous in the literature
and a single unlabeled column would be unusable at analysis time. Both are
`NA_no_data` unless *both* inputs exist.

### Depth and events

| Column | Definition | Source |
|---|---|---|
| `volume` / `open_interest` | Primary long leg | UW `option-contracts` |
| `earnings_days_away` | Calendar days to next report | FMP earnings calendar | **Negative is meaningful** (the report already happened) and is not clamped |
| `time_of_day_bucket` | ET session segment | derived from `signal_ts` | `open` <10:00, `morning` <12:00, `midday` <15:00, `power_hour` <15:45, `close` ≥15:45, else `outside_rth`. **Logged as a feature; never scored** — `test_time_of_day_is_not_an_input_to_the_scorer` pins it |

### Greeks — MODELED, and labeled

| Column | Definition | Source | Formula |
|---|---|---|---|
| `net_delta_modeled` | Structure delta per contract | **Black-Scholes, ours** | `Σ(leg delta × signed qty)`. `NA_no_data` if **any** leg is unpriced — a partial sum looks identical to a complete one |
| `greeks_source` | Provenance | — | Always `black_scholes_modeled`. **No provider in the stack supplies Greeks** (`unusual_whales/client.py`: "UW does not supply greeks"). Per Ruling 2 this stamp is permanent |

### Market regime (P6)

**Market-level, and deliberately not the per-signal tag.** Ruling 2 rejected
substituting a symbol-level tag: two signals fired in the same minute must not
disagree about what market they were in, and the pre-registered per-regime cuts
need classes that exist independently of any signal.

**Join rule — no lookahead.** `market_regime_session` is the most recent session
**strictly before** the signal's date. A signal fired at 10:15 cannot know that
day's close; joining to it would condition the per-regime cuts on the future.

| Column | Definition | Source | Formula |
|---|---|---|---|
| `market_regime_class` | The cut the gate and window-close analysis use | `daily_regimes` | `{vol}_{trend}`, e.g. `highvol_below`. `unknown` if either axis is unmeasured — **never** defaulted to a middle bucket, which would swell one class with rows never measured |
| `market_regime_session` | Which session the regime came from | `daily_regimes` | Strictly prior to `signal_ts` |
| `vix_close` | VIX close that session | FMP `^VIX` | — |
| `vix_percentile_20d` | Percentile of `vix_close` in the trailing 20 sessions, inclusive | derived | `count(v <= today) / 20`. **Coarse by construction** (5% granularity); the 20-session window is the ruling's specification, applied literally. `lowvol` <0.33, `highvol` ≥0.67 |
| `spx_realized_vol_20d` | S&P 500 realized vol | FMP `^GSPC` | Annualised sample stdev of 20 daily log returns × √252 |
| `spx_vs_50d_sma` | Trend position | FMP `^GSPC` | `(close − SMA50)/SMA50`. `above` if ≥0 |
| `regime_tag` / `regime_vol` / `regime_tape` | **Supplementary** per-signal tag | `MarketContext` | Symbol-level: iv_rank × the symbol's own 20-day close z-score. Retained per Ruling 2 but **does not replace** `market_regime_class` |

### Provenance

| Column | Definition |
|---|---|
| `signal_build_sha` | Git SHA of the build that **produced the signal** |
| `export_git_sha` | Git SHA of the build that **produced this CSV** — a different thing |
| `scanner_version` | `scoring_model_version` the decision was scored under. The real model lineage |

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
| `exit_ts` | When the position closed | The **measured** exit instant when the grade replayed minute bars; otherwise the moment the grader ran |
| `exit_ts_is_measured` | Which of those two it is | `True` = real exit time. `False` = grader clock — **do not read as hold time** |
| `exit_price` | Signed structure net at exit, per share | `exit_price_per_share` |
| `exit_price_basis` | Matches entry basis | `actual_fill` / `modeled_mid` |
| `exit_reason` | `profit_target` / `stop_loss` / `time_stop` / `session_close` / `expiry` | Empty for grades that simulated no path |
| `pnl_usd_net` / `pnl_usd_gross` / `costs_usd` | Net, gross, and the difference | — |
| `pnl_pct` / `r_multiple` | Return on defined risk | `pnl_usd_net / abs(max_loss_usd)`. Identical for a defined-risk debit; emitted separately rather than silently merged |
| `hold_minutes` | Entry → exit | Measured from `exit_ts` when available; otherwise derived and **overstates** hold time |
| `elapsed_days` | Entry date → exit date | Trustworthy on every row |
| `outcome_source` | Grading method | `live_close` (real fill) / `managed_policy` (daily marks) / `managed_policy_intraday` (minute bars). **Never pooled** — the daily and intraday grades are different measurements and both are kept |

### Excursion — BOUNDS, not achieved prices

| Column | Definition | Formula |
|---|---|---|
| `mfe` / `mae` | Most favourable / adverse excursion, per share as a fraction of entry | From minute-bar extremes |
| `mfe_ts` / `mae_ts` | When each occurred | — |

**Why bounds.** A minute bar has a high and a low with **no ordering between
them**. For a spread the best case pairs each long leg's high with each short
leg's low; those extremes need not have co-occurred within the minute. So MFE/MAE
bracket what the structure *could* have shown, not a price that was certainly
available.

The same ambiguity is resolved against the strategy on exits: the stop is
evaluated on the bar's **worst** value and checked **before** the target on its
best, so a minute that traded through both books as a **loss**.

### Mark quality — the caveat travels with the grade (P7)

UW minute bars are **trade-driven**: a bar exists only for a minute that printed.
The replay holds through gaps and never interpolates, so an exit that triggered
and reversed inside a gap is **missed, not mispriced**. The error is therefore
**directional** — trades look longer-held than they were, and stop-outs are
under-reported.

| Column | Definition | Formula |
|---|---|---|
| `n_marks` / `bars_observed` | Priced minutes the replay actually saw | — |
| `mark_coverage_pct` | Fraction of RTH minutes observed | `n_marks / RTH minutes in [entry, exit]`, capped at 1.0. Counts weekday RTH without a holiday calendar, so a holiday inflates the denominator and **understates** coverage — wrong in the conservative direction |
| `max_gap_minutes` | Largest run of consecutive unobserved minutes | Consecutive bars are a **0-minute** gap. `NA_no_data` with fewer than 2 marks — one observation cannot evidence continuity, and reporting 0 would claim perfect coverage |
| `grade_confidence` | `high` / `low` / `unknown` | `low` when `max_gap_minutes > 15` |

**Reference measurement** (`SPY260728C00730000`, 2026-07-28, a *liquid* 0DTE):
`n_marks 123 · coverage 31.5% · max_gap 52min · confidence low`.

**0DTE re-enable bar** (Ruling 2): coverage ≥80% of RTH **and** max gap ≤5min, on
a representative sample. The reference measurement **fails** it, which is why
0DTE remains suspended even though intraday marks shipped.

### Cost stress — the H4 gate input

`CAPTURE_WINDOW_PREREGISTRATION.md` §6 makes H4-after-one-tick a **precondition
for live capital**, so these are not footnotes.

| Column | Definition | Formula |
|---|---|---|
| `pnl_at_1tick_worse` | P&L one tick worse on entry **and** exit | `pnl_mid − (legs × 2 × $0.01 × 100 × contracts)`. Both directions: you pay it entering and again exiting |
| `pnl_at_half_spread_worse` | P&L at half-spread worse both ways | `pnl_mid − (half_spread × legs × 2 × 100 × contracts)`. `NA_no_data` rather than guessed when no two-sided minute exists |
| `cost_stress_source` | Where the spread came from | `effective_from_side_volume` (derived from executions: `premium_side / volume_side`) or `nbbo` (a quoted book). **Not interchangeable** — never pooled. Per Ruling 2, quote-derived is primary on new rows and execution-derived is the fallback |

At $0.01 per leg per direction, a 2-leg spread surrenders **$4 round trip** —
4% of a $100 defined-risk cap before any spread at all.

## Bucket-specific columns

| Column | Bucket | Status |
|---|---|---|
| `session_segment_score` | 0DTE | `NA_not_implemented` — no time-of-day weighting exists |
| `gex_proxy` | 0DTE | `NA_not_implemented` — no dealer-positioning input |
| `theta_at_entry` / `vega_at_entry` | LONG | Structure-level, summed from the **modeled** per-leg Greeks (see `greeks_source`). `NA_no_data` unless every leg priced |

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
