# VRP Stage 2 — Pre-Registration (harvestability at k=1.0)

**Committed BEFORE any Stage-2 number was computed.** Inherits
`vrp_preregistration.md`, the Stage-1 result, and the Stage-1 **addendum**
disclosures, which bind here.

## 0. Basis and recorded expectations

- Stage-1 gate: **45d only** (30d sub-material). Run at h=45, tenor band 40-50 DTE.
- **Recorded expectation (stated in advance): the null.** Specifically —
  (a) the ~2.3-pt calm-weighted premium is the same order as the measured ~$24
  round-trip spread tax, so net expectancy at k=1.0 is expected ≈ 0 or negative;
  (b) **short-premium at 45d is expected to LOSE in the 2022 bear cell** (the
  premium itself averaged −2.28 pts there; term structure inverted — structural);
  (c) the §7 prior stands: the one miniature Stage-2 (10-0 at mid) went 0-10 at
  k=1.0.
- **Fragility disclosure:** the bear cell is 5 correlated index-heavy names
  (SPY/QQQ/IWM/AAPL/MSFT), n≈40 entries. Magnitudes there are fragile.
- A large positive result will be treated as a bug until proven otherwise.

## 1. Frozen variant grid (6 variants — no additions after seeing results)

Entries (2) × structures (3), all at h=45:

- **Entries:** `unconditional`; `iv_rich` = ATM-call `contract_trailing_percentile
  ≥ 2/3` at entry (the Stage-1 construct — causal, trailing-only, min 10 prior obs).
- **Structures:** `put_credit_spread`, `call_credit_spread`, `iron_condor`
  (defined-risk only).

**Causal-filter constraint (binding):** no variant may condition on era/regime
labels or any quantity not computable from trailing data at entry. "Avoids 2022"
via the label is look-ahead in a risk-management costume and is forbidden.

## 2. Mechanics (frozen)

- **Entry sampling:** the FIRST trading day per (symbol, expiry) with DTE ∈ [40,50],
  parity spot available, and required strikes present. One entry per
  (symbol, expiry, variant).
- **Strikes:** short leg = nearest ladder strike ≥ 5% OTM (puts: ≤ spot×0.95;
  calls: ≥ spot×1.05); long leg = one ladder step further OTM. Iron condor = both
  verticals opened same day, each managed independently by the same exit rules,
  P&L summed.
- **Fills:** recorded NBBO at **k=1.0** (k=0.5 reported alongside for the
  slippage-fragility read), both legs, open AND close, plus commissions
  ($0.65/contract/leg, both ways) — the existing fill model / real-mark evaluator.
- **Liquidity guard:** entry-day bars must pass the configured BT_* guard
  (min OI 250, min vol 20, max spread 15%); rejects are excluded and counted.
- **Exits (fixed):** profit target = 50% of credit; stop = spread value 2× credit
  (i.e. pnl ≤ −1× credit); time stop = 21 trading days; else expiry/data-end.
- **No tunable thresholds exist** in this grid (all cutoffs fixed above), so
  walk-forward tuning is vacuous by construction; reporting is per-era + pooled
  with cluster bootstrap (symbol × entry-month; quarter-level sensitivity),
  **Bonferroni over the 6 variants** (per-variant CI at α = 0.05/6).

## 3. Report contents (fixed)

Per variant × k: n / wins / losses / win rate / net P&L / expectancy / PF, and the
**§6.1 tail block**: max drawdown (chronological), worst single trade, **CVaR 5%**,
loss-month clustering (do losses arrive together?), per-era expectancy with the
2022 cell explicit, degeneracy guard (<2 losses or single-regime wins →
**uncalibratable**, cannot pass), and the sizing implication (per-trade risk at
which the worst observed cluster survives a $2k book).

## 4. Pass criteria (all required; else fail to reject H0)

1. Pooled net expectancy at k=1.0 > **$5/contract** (pre-registered material
   margin) with Bonferroni-corrected cluster-bootstrap CI excluding zero.
2. Sample includes the 2022 bear and 2025 cells with **real losses present**
   (tail paid), and pooled expectancy stays positive **including** them.
3. Not slippage-fragile (positive at k=0.5 only does not count).
4. Degeneracy guard clear.
5. Verdict written to the registry either way; a null is recorded as
   "premium exists (Stage 1); not harvestable at retail spreads (Stage 2)" —
   which points at execution cost, not at the hypothesis being wrong.

*Research and decision-support only — not investment advice.*
