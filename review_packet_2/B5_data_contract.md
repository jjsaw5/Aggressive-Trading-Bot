# B5 — Data contract, and what the browser computes for itself

Sample payloads captured live from the running instance:

| File | Endpoint |
|---|---|
| `B5_sample_candidate_1_5dte.json` | `GET /short-duration/1-5dte/candidates?limit=2` |
| `B5_sample_candidate_0dte.json` | `GET /short-duration/0dte/candidates?limit=2` — **`[]`**, the bucket is suspended |
| `B5_sample_configuration.json` | `GET /short-duration/configuration` |
| `B5_sample_calibration.json` | `GET /outcomes/calibration` |
| `B5_sample_market_regime.json` | `GET /short-duration/market-regime` |

## Candidate shape (42 fields)

```
catalyst, confidence, contract, data_quality_score, detected_at, direction,
dte_category, engine_pick, entry_allowed, entry_iv, entry_notes, entry_spot,
entry_trigger, exit_plan, expires_at, freshness, id, invalidation, iv_rank,
market_context, max_risk_usd, news_score, order_bracket, pick_rank,
pick_reason, probability_of_profit, regime, reasons, reject_reasons,
reward_to_risk, risk_policy_version, score, scorecard, scoring_model_version,
signal_metadata, state, strategy, symbol, targets, thesis, trade_plan,
what_has_to_happen
```

**Good:** the payload carries its own provenance — `scoring_model_version`,
`risk_policy_version`, `data_quality_score`, `freshness`, `scorecard` (with
`abstained`, `abstain_reason`, `conviction_status`, `input_coverage`),
`reject_reasons`. A client cannot render a score without also having been handed
the reasons not to trust it. That is the right shape.

**Nulls are nulls.** In the captured sample `pick_rank: null`,
`entry_block_reason: null`, `pick_reason: ""` — absent values arrive as absent.
No `0.0` stand-ins.

## `/outcomes/calibration` — nulls all the way down

```json
{"n_decisions": 78, "n_resolved": 0, "n_decisive": 0,
 "win_rate": null, "direction_accuracy": null, "avg_predicted_pop": null,
 "realized_win_rate": null, "calibration_gap": null, "brier_score": null,
 "net_pnl_usd": null, "expectancy_usd": null, "profit_factor": null,
 "score_pnl_spearman": null, "flow_quality_verdict": "insufficient", ...}
```

Zero resolved decisions produces `null` for every derived statistic and
`"insufficient"` for the verdict — not `0.0`, not `0%`, not an empty-but-plotted
chart. The API refuses to compute a statistic it has no sample for. This is the
honesty rule holding at the boundary the UI actually consumes.

## THE FINDING: `confidence` is not an independent reading

Full proof with 58/58 exact reproductions in **`B5_confidence_is_derived.txt`**.

Summary: the board renders `tradability NN/100 · confidence NN% · data quality
NN%` as three peer readings. `app/shortduration/scoring/engine.py:108` defines

```python
overall = round(normalized * (0.6 + 0.4 * data_quality), 4)
```

so `confidence` is `score` shrunk by a linear function of `data_quality` — both
of which are already on the same line. Verified against every row of the live
response: 58/58 reproduce exactly, residual 0.

The number is not wrong. The *implication* is: a reader seeing 77 and 72% agree
believes a second instrument corroborates the first. There is no second
instrument. Adding a `%` makes it read as a likelihood.

## Thresholds the browser invents

These constants exist nowhere but in the HTML. They are not served, not
configurable, not documented, and not shared between the two places that use
them:

| Line | Code | Meaning |
|---|---|---|
| 657 | `p.probability_of_profit < 0.4 ? "neg" : < 0.55 ? "warn" : "pos"` | POP colour, positions view |
| 1022 | *identical expression, duplicated* | POP colour, candidates view |
| 1026 | `drag < 0.15 ? "pos" : drag < 0.30 ? "warn" : "neg"` | cost-drag colour |

**Why this matters for display honesty.** Green at POP ≥ 0.55 is a judgement
about what counts as good odds, made in a stylesheet, on a quantity the same
screen labels UNCALIBRATED. Colour is read faster than text; a green number
survives in memory after the caveat beside it has been skimmed past. The
0.15/0.30 cost-drag bands are the same kind of judgement about what counts as an
acceptable spread tax.

The duplication at 657 and 1022 is the structural risk: two copies of an
unnamed constant that must agree and are not linked.

**Recommendation:** serve these bands from config alongside the values they
colour, so the threshold is versioned with the model rather than living in
presentation, and so the two views cannot drift.

## Time zones

Every timestamp renders through `toLocaleString` / `toLocaleTimeString` /
`toLocaleDateString` with no `timeZone` option (lines 446, 763, 796, 817, 1185,
1220, 1252, 1286, 1299, 1461, 1681). These are **browser-local**, not ET.

One exception, and it is instructive: line 1562 passes `timeZone: "UTC"`
explicitly for the calendar month label — so the codebase knows the option
exists and uses it exactly once.

For a product whose entire subject is 0DTE and 1–5DTE expiries, "09:31" meaning
different instants on different machines is a correctness hazard, not a polish
item. Session boundaries, RTH coverage and expiry are all ET concepts.
**Recommendation:** pin `timeZone: "America/New_York"` and suffix the label
`ET`.

## Console

`B2_console_errors.txt` — one 404 on a resource fetch across all 12 screens.
No JS exceptions, no unhandled rejections.
