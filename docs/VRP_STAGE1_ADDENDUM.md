# VRP Stage 1 — Addendum: auditor verifications (run before Stage-2 pre-registration)

Four checks requested in the auditor review of the Stage-1 result, each resolved
against the data (not assumption). These are disclosures that bind Stage 2.

## 1. Bootstrap clustering — robust to coarser blocks

The pre-registered cluster was **(symbol, month)** — not symbol-only. Re-run with
coarser overlapping-window blocks:

| h | sym×month | sym×quarter | sym×half-year | excludes 0? |
|---|---|---|---|---|
| 30d | [+0.65, +2.68] | [+0.68, +2.70] | [+0.70, +2.67] | yes, all |
| 45d | [+1.40, +3.19] | [+1.31, +3.32] | [+1.29, +3.31] | yes, all |

The CIs barely move as clusters coarsen 368 → 149; the existence claim is robust to
the overlap concern. (The *material-margin* claim is a different matter — see #3.)

## 2. The 2022 cell is 5 correlated names, selected for data cleanliness

**SPY, QQQ, IWM, AAPL, MSFT** — three index ETFs plus the two megacaps that dominate
those same indexes. They are in the cache because the corpus build chose split-free
names for OCC ladder continuity — a data-cleanliness selection, not a scan-driven
one, but a selection channel into the most decision-relevant cell all the same.
**The −2.28-pt bear reversal is directionally credible; its magnitude is fragile**
(5 correlated names, one regime, n=140). Recorded as such.

## 3. The pooled number is calm-weighted — equal regime weighting flips the gate's story

| h | pooled (obs-weighted) | **era-equal-weight** | per-era |
|---|---|---|---|
| 30d | +1.71 pts | **+1.66 pts** | +0.94 / +1.10 / +2.95 |
| 45d | +2.34 pts | **+1.09 pts** | **−2.28** / +3.16 / +2.39 |

Under equal regime weighting, **45d falls to +1.09 pts — below the 2.0 material
margin — while 30d is the stabler premium (+1.66, positive in every era).** The
pre-registered gate (obs-weighted pooling) passed 45d and failed 30d; the auditor's
irony is confirmed quantitatively: **the gate selected the horizon with the higher
average reward and the worse stress behavior.** The gate stands as registered — but
Stage 2 inherits this as recorded prior evidence, not a footnote.

## 4. The mechanism is confirmed: the term structure inverted in the bear

Era-mean ATM IV by tenor band:

| Era | 30d-band IV | 45d-band IV | slope (45−30) |
|---|---|---|---|
| **2022 bear** | 0.307 | 0.291 | **−1.7 pts INVERTED** |
| 2023-24 | 0.242 | 0.266 | +2.5 pts normal |
| 2025 | 0.267 | 0.291 | +2.3 pts normal |

In the bear, front IV was bid above back while realized vol ground higher — the 45d
tenor was structurally under-compensated, exactly the hypothesized mechanism. **The
sign flip is structural, not sampling noise.** Which is more concerning, not less:
it will recur in the next sustained bear.

## Consequences bound into Stage 2 (pre-registered there)

- **Expectation: Stage 2 fails in the bear at 45d.** Stated in advance.
- **The binding constraint is bear survival, not pooled expectancy.**
- **Causal-filter constraint:** any regime/conditioning variant must be computable
  at entry from trailing data only. Era labels are forbidden as filters — a filter
  that "avoids 2022" using the label is look-ahead in a risk-management costume.
- **Cost bridge (order of magnitude):** 2.3 vol pts ≈ $25–40/contract of theoretical
  edge on a defined-risk 45-DTE vertical vs a measured ~$24 round-trip spread tax on
  comparable structures. The premium and the transaction cost are the same size;
  Stage 2's outcome will be decided by structure/cost choices, not signal quality.
