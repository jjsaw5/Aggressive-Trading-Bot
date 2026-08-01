# Signal-Only Capture Window — Pre-Registration

**Committed before the first captured signal. Dated 2026-07-31.**
Basis: remediation directive responding to the audit of build `7afa098`.

This document exists so the analysis at window close cannot be chosen after
seeing the data. Everything below — window length, hypotheses, statistics,
thresholds — is fixed now. Any deviation must be recorded as an amendment with
its own date and reason, below the signature line, not by editing the text above
it.

---

## 1. Why this window exists

The audited corpus cannot answer whether the scanner has an edge:

- 67 scanner signals over **4 trading sessions** (Jul 23, 24, 27, 28), all under
  `sd-scoring-2026.07-v3`.
- **52/67 (78%) bearish**, into a week where breadth strengthened. Effective
  sample size is far below nominal — the outcomes reflect one repeated direction
  call, not 67 independent selections.
- Score↔outcome discrimination indistinguishable from zero.
- Market context absent: `spot_price` 0.0 on all 67; NBBO, IV, iv_rank, cost
  drag, Greeks and earnings all computed live and discarded.
- All 38 0DTE signals resolved `expiry`, because daily marks cannot see an
  intraday exit — the managed policy those rows claim was never measured.

**The current record cannot distinguish a bad model from a bad week.** Tuning on
it fits noise. So: freeze the model, fix the instrumentation, collect clean data.

## 2. What is frozen

`sd-scoring-2026.07-v3` is frozen for the duration. **No changes** to component
weights, thresholds, scoring components, or the watchlist universe.

Permitted during the window (none of which alter what the scorer computes):
data persistence, grading integrity, logging/decomposition, bug fixes to
non-scoring code.

## 3. Window length — fixed stopping rule

**Minimum 8 weeks of signal-only capture**, extended until BOTH:

- **(a)** ≥100 resolved scanner signals per active bucket, and
- **(b)** ≥2 distinct regime tags with n≥15 each (per the regime pipeline).

**The window does not end early on good news.** No interim analysis of the
primary hypotheses will be performed or reported. Operational health checks
(row counts, null rates, coverage) are permitted and are not results.

Active buckets at window open: **1-5DTE** and **Medium**. 0DTE is suspended and
re-enters only after NBBO persistence and intraday marks ship; if it re-enters
mid-window, its 100-signal count starts from its re-enablement date.

## 4. Hypotheses — stated now

| ID | Hypothesis | Null |
|---|---|---|
| **H1** | Composite score ranks realized R within a bucket | ρ = 0 |
| **H2** | `predicted_pop` is calibrated | Reliability deviates from the diagonal |
| **H3** | Direction calls beat a coin flip | hit rate = 50% |
| **H4** | Expectancy is positive net of measured cost drag | E[R] ≤ 0 |
| **H5** | Selection scoring adds value *within* one direction | top and bottom quartile equal |

H5 is the decomposition question the audit raised: outcomes were driven by *which
way* the scanner leaned, not *which contract* it picked. H3 and H5 separate those.

## 5. Statistics — specified now

1. **H1** — Spearman ρ(composite_score, realized R) per bucket, with a
   percentile bootstrap 95% CI (2000 resamples, fixed seed). Reported per bucket;
   no pooling across buckets.
2. **H2** — `predicted_pop` binned into deciles: reliability table, Brier score,
   and n per bin. Bins with n<20 flagged insufficient, not silently merged.
3. **H3** — direction hit rate vs 50% with a binomial 95% CI, **and** vs the
   period's base rate (share of sessions the underlying closed in the signalled
   direction), per bucket and per regime tag.
4. **H4** — mean R and total P&L per bucket, computed three ways: at stored mid,
   at 1 tick worse on entry and exit, and at half-spread worse. All three
   reported; the 1-tick figure is the headline.
5. **H5** — within same-direction signals only, mean R of top-quartile vs
   bottom-quartile Selection score, with a bootstrap CI on the difference.

**Multiplicity:** five primary hypotheses. Any single-hypothesis pass is
reported with a Holm-corrected as well as an uncorrected p-value, and the
corrected value governs.

## 6. Gate thresholds — fixed now

Live capital on scanner signals requires **all** of:

- Conviction gate **GREEN**, including `per_regime` and `validated_feature`.
- **H4 passes** in the bucket to be traded: expectancy > 0 **after the 1-tick
  cost stress**, CI excluding zero.
- Either **H1** or **H5** passes corrected — the engine must demonstrate it ranks
  *something*, not merely that a direction call happened to work.

Failing any of these, capital stays off. **If the gate is still RED after a clean
window, the conclusion is that the current model has no edge** — and the next
step is redesign informed by the H3 direction data, not a weight tweak.

## 7. What would falsify the whole approach

Stated so it cannot be rationalised away later:

- H3 at ~50% with a tight CI ⇒ the direction engine is noise. Selection scoring
  cannot rescue it, and the composite's market-facing components are worthless as
  built.
- H1 and H5 both null with adequate n ⇒ the score carries no ranking information
  at any level. That is a redesign trigger, not a re-weighting trigger.
- H4 negative with the mid-price figure positive ⇒ any apparent edge is smaller
  than the spread. At a $100 per-trade cap that is a structural finding about
  account size, not about the model.

## 8. Amendments

None. Any change to sections 3–7 after the first captured signal must be recorded
here with its date and reason, and the analysis must report both the original and
amended plan.

---

*Committed 2026-07-31, before the first signal captured under the new pipeline.*
