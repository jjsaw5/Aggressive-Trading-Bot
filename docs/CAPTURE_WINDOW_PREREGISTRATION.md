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

Any change to sections 3–7 after the first captured signal must be recorded here
with its date and reason, and the analysis must report both the original and
amended plan.

---

*Committed 2026-07-31, before the first signal captured under the new pipeline.*

---

### Amendment 1 — 2026-08-01 — model version `sd-scoring-2026.07-v3` → `sd-scoring-2026.08-v3.1`

**Recorded before the first captured signal.** Zero signals had been captured at
the time of this amendment: production was still running build `7afa098` and the
window had not started. Section 2's freeze is therefore not breached — there is
no corpus to split, and nothing here was chosen after seeing an outcome.

**What changed.** `IVContext.term_structure_slope` is now populated by the live
Unusual Whales provider, from `GET /api/stock/{ticker}/volatility/term-structure`.

**Why it is a version change even though no weight moved.** Weights, components
and thresholds are byte-identical to v3. But `app/shortduration/scoring/
components.py:137` has read `term_structure_slope` since v3 and applies a 0.85
multiplier to the volatility component when the structure is backwardated
(front IV richer than back — IV-crush risk for a debit buyer). No live provider
ever populated the field, so **that penalty had never once fired in production**,
while the mock provider did populate it and so fired it in every test run. The
tested model and the shipped model were not the same model. The version records
that the shipped one has changed, not that the code has.

Measured effect on the golden reference: a backwardated term structure scores
**50.6 against a 52.1 baseline** — a 1.5-point penalty that production signals
can now receive and previously could not.

**Why now rather than during or after the window.** This is the only moment the
change is free. Mid-window it would split the corpus; post-window it would leave
8–12 weeks of data collected under a model whose behaviour differed from the one
under test. The alternative — deleting the branch — would discard a sound
IV-crush guard to preserve a wiring bug.

**Sections 3–7 are unchanged.** Window length, hypotheses, statistics, gate
thresholds and falsification criteria all stand exactly as originally committed.

**Controls added with the change** (so this class of defect is mechanically
detectable rather than found by audit):
- `tests/test_scoring_golden.py` — golden-file regression over the scorer:
  fixed inputs, asserted composite outputs.
- `tests/test_provider_scoring_contract.py` — pins which provider fields reach
  the scorer. Against the pre-fix provider, 8 of its 9 tests fail; against the
  fixed provider, all 9 pass. That is the demonstration that the control catches
  this defect class.

**Origin:** self-reported as FINDING_01 in the audit packet of 2026-08-01, then
ruled on by the reviewer (Ruling 1). Recorded here per this section's own
mechanism.

---

### Amendment 2 — 2026-08-03 — `sd-scoring-2026.08-v3.1` → `sd-scoring-2026.08-v4.0`

**Recorded before the first captured signal.** Zero signals had been captured at
the time of this amendment: production had not been redeployed and the window had
not started. Nothing here was chosen after seeing an outcome.

**A MAJOR version, not a point release.** v3 → v3.1 changed whether a coefficient
could fire. This changes *which instrument a signal is expressed in*. A v3.1 row
and a v4.0 row on the same symbol, same direction, same minute are different
trades. They must never pool.

#### What changed

Contract selection now prices **probability**, not only payoff.

1. **POP enters the fit function.**
   `app/engine/contract_selection.py`

   ```
   before:  fit = min(1, rr/2) * 0.7  +  min(1, width/strike*20) * 0.3
   after:   fit = pop * 0.45  +  min(1, rr/2) * 0.35  +  min(1, width/strike*20) * 0.20
   ```

2. **A hard POP floor of 0.25 REJECTS** rather than down-ranks
   (`SelectionConfig.min_pop`). Odds that cannot be modelled do **not** clear it —
   an unmodellable probability is not evidence of a good one.

3. **The short leg gets a delta floor of 0.15** (`min_short_leg_delta`).

4. **Horizon is decided by the thesis, not the strategy label**
   (`contracts.short_horizon_viable`). A trend thesis is released to the 1–5 DTE
   window only when the market's own expected move over that window covers the
   distance to invalidation — i.e. the argument is settled inside it. Otherwise
   the 21–45 DTE default from item 1.11 stands.

#### Why

The selector maximised reward-to-risk, and reward-to-risk is maximised by buying
the cheapest, furthest-OTM spread the chain allows. Both terms of the old fit
function rose as the structure moved away from the money, and the short leg was
bounded only by liquidity, so nothing opposed the drift.

Measured on 2026-08-03, across 64 priced structures: **median POP 0.287, median
R:R 7.79:1, and 45% of all structures below POP 0.25.**

The system recorded, for the SPY structure it chose:

```
POP = 0.1044   R:R = 26.62
"SPY must rise 4.4% to $790.90 by expiry (28d) just to break even."
```

It computed that probability, stored it, displayed it on the board — and did not
use it to choose the contract. A window captured under v3.1 would have measured a
long-shot generator, and a low hit rate would have been unattributable between
the *signal* and the *contract selector*.

#### Sections 3–7 unchanged

Window length, hypotheses, statistics, gate thresholds and falsification criteria
all stand exactly as originally committed.

#### Golden-file delta

`tests/golden/scoring_v3.json` regenerated. **The only change is the recorded
`model_version` string — 9 leaves, no computed value moved.**

This is the same structural limitation reported for Ruling 1 under Amendment 1:
the golden file scores fixed `IVContext` fixtures and passes no trade plan, so a
*selection* change cannot move its numbers. It is reported here rather than
worked around. The controls that DO cover this class are `tests/
test_contract_selection_amendment2.py` (new, direct) and the freeze path guard,
which now covers the selection paths — see below.

#### Control added with the change

`app/engine/contract_selection.py` and `app/shortduration/contracts.py` were **not
in the freeze guard's `GUARDED` regex** when this change was written.
`scoring/components.py:181` reads `reward_to_risk` off the *selected plan*, so
changing selection changes a scored component and therefore the shipped model —
with no diff under `scoring/` at all. That is FINDING_01's shape in a new file,
and it was found by accident rather than by a control.

Both paths were added to `GUARDED` in a **separate, earlier commit**, deliberately
landed before this one so that this change had to clear the guard rather than
arriving alongside the fix that would have caught it.

**Origin:** raised by the user on 2026-08-03 ("why do we not see the app surface
plays like this?"), written up as
`docs/PROPOSED_AMENDMENT_2_CONTRACT_SELECTION.md`, approved in full (C1–C4) with
the guard fix ordered first and independent.
