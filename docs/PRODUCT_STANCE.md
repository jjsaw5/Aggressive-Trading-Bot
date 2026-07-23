# Product stance — a truth-teller, not a picker

This is a decision of record, backed by evidence, about what this product is.

## What the evidence says

Three independent lines of analysis, all on real UW marks, net of cost:

1. **Full-cycle real-mark backtest** (`CORPUS_REAL_MARK_2021_2026.md`) — the engine's
   own picks: −$49/trade at full spread, regime-conditional (loses badly in
   selloffs, positive only in up-tapes).
2. **Pre-registered flow experiment** (`FLOW_EXPERIMENT_DISPOSITION.md`) —
   fail-to-reject the null; flow-as-confirmer is an up-tape artifact.
3. **Out-of-sample feature validation**, pricing (`FEATURE_VALIDATION_RESULT.md`) and
   flow (`FLOW_FEATURE_VALIDATION_RESULT.md`) — **empty registries**; not one feature,
   flow included, clears a Bonferroni-corrected out-of-sample bar. The core
   "smart-money-is-buying" signal (at-ask aggression) is if anything negative
   (chasing). One thread survives suggestively but unproven (sweep intensity).

**Conclusion: there is no demonstrable net-of-cost edge in this data. Every layer
reduces to directional beta + the spread tax.**

## What the product therefore is

A **defined-risk cost & probability calculator** for short-duration options. For any
structure it tells you, honestly:

- **Probability of profit** (market-implied, from IV) — or says POP is uncomputable.
- **Cost-drag** — the round-trip spread tax as a share of your defined max-loss.
- **Tradability rank** — liquidity, defined risk, structure sanity. A rank of how
  *cleanly tradeable* an expression is — explicitly **not** a predicted winner.
- **Regime + "what has to happen"** — the context and the move the trade needs.
- **Reversal risk / wrong-instrument warnings** — the read-before-you-act checks.

## What it is NOT, and will not pretend to be

- It does **not** assert conviction. Every score displays **UNCALIBRATED** until a
  feature clears the validation gate; today none has, so it stays UNCALIBRATED.
- It does **not** rank by predicted edge (there is none to rank by). It ranks by
  tradability and shows you the odds and costs.
- It does **not** place trades. Research/paper only; the execution double-gate is off.
- The **thesis is yours.** The tool makes your idea cheaper to evaluate and harder to
  fool yourself about — it does not supply the idea.

## If this changes

The machinery to earn conviction exists and stays wired: add features, expand the
corpus, and re-run `scripts/validate_features.py` / `validate_flow_features.py`. If
something ever clears the gate, the registry fills, weights become data-derived, and
the UI can honestly upgrade from UNCALIBRATED to CALIBRATED. Until then, the honest
label is the product.
