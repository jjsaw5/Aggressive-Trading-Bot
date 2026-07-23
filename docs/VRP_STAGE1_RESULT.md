# VRP Stage 1 — the premium exists, is small, and REVERSES in the bear

Run exactly as pre-registered (`vrp_preregistration.md`, committed first;
machine-readable results in `vrp_stage1_report.json`; deterministic, seed 12345).
Tenor-matched per-contract ATM IV vs forward realized vol; n=1,275 (30d) and
n=1,413 (45d) observations across 25 names, eras 2022-bear / 2023-24 / 2025.

## Headline numbers

| | h=30d | h=45d |
|---|---|---|
| Pooled mean `vrp_vol` | **+1.71 vol pts** | **+2.34 vol pts** |
| Cluster-boot 95% CI (mean) | [+0.65, +2.68] | [+1.40, +3.19] |
| Median | +2.29 pts | +3.24 pts |
| Left tail p05 / p01 / min | −10.5 / −19.1 / −54.1 pts | −9.7 / −33.3 / −43.2 pts |
| **2022 bear mean** | **+0.94 pts** | **−2.28 pts** |
| 2023-24 recovery | +1.10 pts | +3.16 pts |
| 2025 drawdown+chop | +2.95 pts | +2.39 pts |
| Tenor-mismatch manufactures | −0.44 pts | +0.38 pts |

## Pre-registered H1a gate

- **h=30d: FAIL.** CI excludes zero (the premium is real) but the mean (+1.71 pts)
  is **below the pre-registered 2.0-pt material margin**. Recorded as sub-material.
- **h=45d: PASS.** CI excludes zero, +2.34 pts ≥ 2.0, and the tenor-sensitivity
  check shows the premium is **not** a term-structure artifact (mismatch alone
  moves it by ±0.4 pts against a +2.3-pt effect).

**Stage 2 is therefore formally gated open at h=45 only.**

## The three findings that matter more than the gate

1. **The premium reverses in the bear — quantified, as the spec demanded.** At 45d,
   VRP averaged **−2.28 vol pts across the 2022 bear** (n=140): option sellers were
   *under*-compensated — realized vol systematically exceeded what implieds priced.
   The premium is a calm-regime phenomenon. Any harvest strategy collects in calm
   years and pays it back (plus tail) in the regime you most need it to survive.
   This is the insurance analogy made literal: the 2022 cell is the claims year.
2. **The distribution is the classic vol-selling shape.** Median > mean everywhere:
   steady small positives punctuated by rare catastrophic negatives (worst single
   observation −54 vol pts; p01 ≈ −19 to −33). One bad month erases many months of
   premium. Stage 1 measures the premium *gross* — it pays none of the spread, none
   of the tail management. This shape is exactly why §6.1 exists.
3. **Mild time-decay at 45d:** +3.16 pts (2023-24) → +2.39 pts (2025). Consistent
   with a well-known, increasingly crowded premium. Not decisive, worth tracking.

## Verdict against the spec

- **H1a (premium exists): supported at 45d, sub-material at 30d.** The measurement
  is clean: causal (trailing-only percentiles, asserted tenor match), cluster-robust
  CIs, tenor artifact ruled out.
- **This does NOT mean it's harvestable.** Stage 1 is arithmetic on marks nobody
  paid spread on. The §7 prior evidence stands: the one miniature Stage-2 we have
  (10-0 at mid → 0-10 at k=1.0) says the spread tax can consume ~2 vol points
  easily. Stage 2's expectation remains explicitly LOW.
- **Product note (§8) applies:** a 45d finding argues about a *swing credit book*,
  not the 0DTE/1-5DTE product. Nothing here validates the short-duration path.

## What Stage 2 must now do (per spec, before running)

Pre-register its own small variant grid (unconditional / trailing-pct-conditioned
entries × put-credit / call-credit / iron-condor at 45d), fixed exits, k=1.0 real
marks both legs + commissions, walk-forward both directions, and §6.1 tail
characterization — **with the 2022 bear cell in-sample as the mandatory stress**.
Given finding #1, the pre-registered expectation is that Stage 2 fails *in the
bear* even if it collects in calm regimes; a variant that has not survived 2022 is
uncalibratable by the degeneracy guard.

*Research and decision-support only — not investment advice.*
