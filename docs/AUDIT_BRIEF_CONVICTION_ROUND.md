# Audit brief — Conviction Scanner round (Layer-1, validation, truth-teller reframe)

**Scope:** short-duration options scanner (0DTE / 1–5DTE) on `Aggressive-Trading-Bot`.
**Branch / PR:** `claude/options-trading-platform-cl6v83` → PR #23 (base `main`).
**Provider stack under test:** FMP (market/intraday/calendar) · Unusual Whales
(flow, chain, IV, historic) · Benzinga (news) · Robinhood (live positions, read-only).
**Trading posture:** research/paper only; live-execution double-gate remains OFF. No
change to that in this round.

This brief is for the auditor to scope the next round. It states what shipped, the
empirical findings and how they were derived, the one signal still worth chasing, a
data-integrity bug found and fixed, the honest limits, and the open decisions.

---

## 1. What shipped this round

| Area | Change | Commit |
|---|---|---|
| Layer-1 honesty | Score stamped `UNCALIBRATED`, reframed as a **tradability** rank | `aa60756` |
| Layer-1 arming | No ARM without computable POP; 0DTE asserts no conviction; cost-drag rank | `c8fb10f` |
| Corpus | Real-mark backtest extended to 2021→2026, split-free wide universe | `489b496`, `7242923` |
| Feature validation | OOS harness + registry (walk-forward, bootstrap CI, Bonferroni, per-regime) | `5ae0c5d` |
| Flow validation | Flow features + flow-corpus validator (the app's premise) | `3df7555`, `5bf1f79` |
| Truth-teller reframe | UI + `PRODUCT_STANCE.md` — calculator, not picker | `77c3b66` |
| Data-integrity fix | Joined IV-rank history into the short-duration scan | `14101df` |

---

## 2. Core empirical finding — no demonstrable net-of-cost edge

Three independent lines, all on **real UW marks, net of commission and spread**,
converge on the same verdict: **every strategy layer reduces to directional beta +
the spread tax.** There is no out-of-sample, cost-aware edge in the data tested.

### 2a. Full-cycle real-mark backtest (engine's own selection)
`docs/CORPUS_REAL_MARK_2021_2026.md` · n=218 · label = net P&L at k=1.0 (full spread)

| Slice | exp @mid (k=0.5) | exp @full-spread (k=1.0) | win | PF |
|---|---:|---:|---:|---:|
| Pooled | −$30.70 | **−$49.50** | 47% | 0.73 |
| 2021-22 selloff | −$111 | −$132 | 35% | 0.42 |
| 2023-24 recovery | +$4 | −$9 | 52% | 0.94 |
| 2025-26 up-tape | +$32 | +$8 | 56% | 1.05 |

Sign is **regime-conditional**: negative pooled, positive only in the up-tape. Only
`call_debit_spread` (bullish, long premium) is positive by structure.

### 2b. Feature validation — pricing/vol/structure/cost
`docs/FEATURE_VALIDATION_RESULT.md` · n=218, 159 OOS · 7 features · **none validated.**
Every Bonferroni-corrected OOS CI straddles zero. The features with positive point
estimates (direction, structure, momentum) flip sign across regimes.

### 2c. Feature validation — flow (the app's premise)
`docs/FLOW_FEATURE_VALIDATION_RESULT.md` · 25 names, 2023-2025 · n=305, 236 OOS · 12
features · **none validated.** Two results matter beyond the null:
- **`flow_at_ask` is negatively tilted (−0.12).** Buying what others aggressively lift
  at the offer is *chasing* — you pay up and the spread tax finishes it. Sign is the
  opposite of the "smart-money-is-buying" thesis.
- **`flow_sweep` is the one thread that survives** — see §3.

**Method (identical across 2b/2c, no lowered bar):** walk-forward with purge + embargo;
predictor fit on TRAIN only (numeric oriented by train sign; categorical target-encoded
on train means); forward walk → bootstrap CI, reverse walk → independent same-sign
check; Bonferroni over the feature family; per-regime sign-flip guard. Validated
features get **data-derived weights**; nothing validated → **empty registry** (the
honest default that keeps the UI UNCALIBRATED). Deterministic (seeded).

---

## 3. The one live thread — `flow_sweep`

The single most promising signal in the entire study, and where the next round has the
best chance of finding something real:

- **Sweep intensity on the option being bought** was the **only** feature with a
  **sign-consistent positive** OOS read across both walk directions (**+0.136 forward,
  +0.146 reverse**) **and no regime flip.**
- It still **fails** the Bonferroni-corrected bar: CI **[−0.05, +0.32]** includes zero.
  So it is **not validated** — suggestive, not proven.

**Recommended next-round test (pre-register before running):** larger flow corpus
(more names / more entry dates to lift n and tighten the CI) and smarter encodings —
`sweep × at-ask × opening-position`, direction-aware, possibly intraday-timed. The
effect size (~0.14) is exactly what more power could confirm or kill. Treat a single
positive as unproven until it survives the same harness at higher n.

---

## 4. Data-integrity finding (found + fixed this round)

**Bug:** the short-duration scan fetched only the spot IV level (`get_iv_context` →
iv30) and **never joined the IV-rank history**, so `iv_rank`/`iv_percentile` were
always `None` for every symbol on the board. Impact: blanked the volatility factor,
capped data quality (the "83% · missing/stale: IV context" symptom), and — under the
new Layer-1 rules — left POP uncomputable so candidates could not arm.

**Root cause:** wiring gap unique to the short-duration path. The funnel/tier-2
pipeline already computes IV rank via the shared `build_iv_context` helper; detection
just never called it. The data was always available (UW returns a full year of IV
history; JPM resolves iv_rank ≈ 0.22 / iv_percentile ≈ 0.18).

**Fix (`14101df`):** wired the same `build_iv_context` into `detection._score_symbol`
(true IV history preferred, realized-vol proxy fallback). Regression-tested.

**Auditor note — data-quality gaps are the credibility risk.** Verified JPM live: 7 of
8 score inputs were healthy; the 1 broken input (IV rank) was the highest-impact one.
The next round should add an explicit **input-coverage monitor** (per-symbol, per-feed:
present / missing / stale) so a silent feed gap can't quietly degrade scores again. The
score's honesty depends entirely on input integrity.

---

## 5. Honest limits (what the auditor should weigh)

- **Power.** Pricing n=218 / flow n=305, OOS 159 / 236, Bonferroni over 7–12 features
  is a demanding bar. `flow_sweep`'s ~0.14 effect could be real but underpowered. The
  pricing point estimates sit near zero (absence of a strong edge, not merely low
  power); flow is more of a power question.
- **EOD granularity.** Backtest + flow features are daily aggregates. **Intraday flow
  timing — the app's real live signal — is untestable on this feed.** A validated live
  edge could still exist intraday and be invisible here. State this limit explicitly.
- **Universe.** Split-free names only (splits break OCC strike continuity). Widening
  needs split-adjusted contract handling.
- **Flow history coverage.** UW historic flow fields populate ~2023+; pre-2023 is
  NBBO/IV-only. Flow features are only testable on 2023+ data.

---

## 6. Open decisions for the next round

1. **Chase `flow_sweep` or not?** The only non-null thread. One more pre-registered,
   higher-power pass (larger corpus + smarter encodings) to confirm or kill it.
2. **Intraday flow.** The daily feed can't test the live signal. Is an intraday
   historical flow source available/affordable to test the actual premise?
3. **Layer-2 calibration gate.** Currently pointless to build (empty registry → it
   would correctly stay dark). Build only after/if a feature validates.
4. **Input-coverage monitor** (§4) — recommended regardless of direction; protects
   score integrity.
5. **Product framing.** This round committed to truth-teller (`PRODUCT_STANCE.md`).
   Confirm the auditor concurs before further UI/feature work assumes it.

---

## 7. Reproduction + artifact index

```
# corpus backtest (needs UW historic entitlement)
UW_HISTORIC_ENABLED=true UNUSUAL_WHALES_API_KEY=… python -m scripts.real_mark_backtest --mode both --preset all
# pricing feature validation
UW_HISTORIC_ENABLED=true UNUSUAL_WHALES_API_KEY=… python -m scripts.validate_features --preset all
# flow feature validation (reads flow_experiment cache)
python -m scripts.validate_flow_features
```

Artifacts: `CORPUS_REAL_MARK_2021_2026.md`, `FEATURE_VALIDATION_RESULT.md` +
`feature_registry.json`, `FLOW_FEATURE_VALIDATION_RESULT.md` +
`flow_feature_registry.json`, `PRODUCT_STANCE.md`, `FLOW_EXPERIMENT_DISPOSITION.md`.

## 8. Operational security (action required)

The live UW API key and Turso tokens used for the analysis runs were exposed in the
working session transcript. They were used read-only and never committed, but **must be
rotated** before the next round.
