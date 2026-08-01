# Governance — read before changing anything

This file exists because it did not. The requirement to keep a session log was a
standing rule that lived only in session context and was never committed to this
repository, so it was never followed and nobody could see that it wasn't. That is
the failure mode this file is here to prevent: **a rule with no durable home is
not a rule.**

---

## 1. What this product is

A **defined-risk cost and probability calculator** for short-duration options.
Not a picker. See `docs/PRODUCT_STANCE.md`, which is a decision of record backed
by three independent lines of evidence.

- It does **not** place trades. The execution double-gate is off and the
  conviction gate denies execution while RED (it is RED).
- Every score displays **UNCALIBRATED** until a feature clears out-of-sample
  validation. None has. The registries are empty.
- The thesis is the human's. The tool makes it cheaper to evaluate.

**Never make the system place, cancel, or modify a live order.** Brokerage
access is read-only.

## 2. The scoring model is FROZEN

`docs/CAPTURE_WINDOW_PREREGISTRATION.md` §2 freezes the scoring model for the
duration of the signal-only capture window.

**Current model:** `sd-scoring-2026.08-v3.1`
**Freeze point:** tag `freeze/sd-scoring-2026.08-v3.1` = commit `80eb42c`
(see `docs/FREEZE_POINT.md`, which records the SHA so the check works even where
the tag is unavailable)

Forbidden during the window: changes to component weights, thresholds, scoring
components, or the watchlist universe.

Permitted: data persistence, grading integrity, logging and decomposition, and
bug fixes to non-scoring code.

**The freeze is about BEHAVIOUR, not about which files you edited.** FINDING_01
proved this the hard way: a scoring input (`term_structure_slope`) sat unread by
any live provider for the whole life of v3, so a penalty in the frozen scorer
could never fire in production while firing in every test. Populating it — an
edit entirely outside `app/shortduration/scoring/` — changed the shipped model.

Three controls enforce this. Do not disable any of them:

| Test | Catches |
|---|---|
| `tests/test_scoring_freeze.py` | scoring modules importing capture-only data; a silent version bump |
| `tests/test_scoring_golden.py` | any change to what the scorer computes for fixed inputs |
| `tests/test_provider_scoring_contract.py` | any change to which provider fields reach the scorer |

If one fails, the question is **not** "how do I make this pass". It is "does this
change end the capture window?" A legitimate change requires, in the same commit:
a `scoring_model_version` bump, a dated amendment under
`CAPTURE_WINDOW_PREREGISTRATION.md` §8, and a regenerated golden file with the
delta documented.

## 3. Session log — REQUIRED

Every working session appends an entry to `docs/audit/SESSION_LOG.md`:

- What changed and why
- PRs opened or merged
- Decisions taken, with their reasoning
- A **DEVIATIONS** section — write `None` explicitly when there are none, so the
  absence is a claim rather than an oversight

## 4. Honesty rules that are not negotiable

These are the product's actual value proposition; violating one is worse than
shipping nothing.

- **Absent stays absent.** Never substitute `0.0` for a missing measurement. A
  required float plus an `or 0.0` fallback is how 67 of 67 audited signals came
  to report a spot price of zero.
- **Modeled is labeled.** Greeks are Black-Scholes (no provider supplies them)
  and carry `greeks_source`. Cost stress carries `cost_stress_source`.
- **Sentinels, never blanks.** Exports use `NA_not_implemented` (no such
  concept), `NA_no_data` (concept exists, this row lacks it), `NA_unresolved`.
  The distinction between the first two is load-bearing.
- **Report gaps, don't approximate them.** "Anything the system cannot produce
  should be reported as a gap, not approximated. The gaps are as informative as
  the data."
- **Bounds are named as bounds.** MFE/MAE come from bar extremes that have no
  ordering within the bar; they are not achieved prices.
- **Ambiguity resolves against the strategy.** A minute bar that traded through
  both the stop and the target books as a loss.

## 5. Risk limits — hard

`docs/RISK_POLICY.md`. Capital preservation first; aggressive growth is pursued
*within* these, never by loosening them.

$100 max risk per trade · $300 aggregate account heat · 4 concurrent positions ·
20 contracts per trade.

## 6. Secrets

Secrets live only in `.env`, which is gitignored. Never commit one, never print
one, never paste one into a chat transcript or a PR body. Secret-scan every
staged diff before committing.

## 7. Pre-registration discipline

Experiments are pre-registered before results exist
(`docs/*preregistration*.md`). Changing an analysis plan after seeing results is
forbidden. Post-hoc reviews go in separate `*_RESULT` / `*_DISPOSITION`
documents so the frozen design stays untouched.

## 8. Governing documents

| File | Authority |
|---|---|
| `docs/CAPTURE_WINDOW_PREREGISTRATION.md` | **Binding now** — the freeze, stopping rule, hypotheses, gate thresholds |
| `docs/PRODUCT_STANCE.md` | What this product is and is not |
| `docs/RISK_POLICY.md` | Hard limits |
| `docs/OUTCOMES.md` | Snapshots immutable; never rewritten after the fact |
| `docs/METHODOLOGY.md` | How scoring and grading work |
| `docs/*preregistration*.md` | Frozen experiment designs |

## 9. Known issues

- `docs/RISK_POLICY.md` contradicts itself: the limits table sets
  `MAX_TRADE_RISK_PCT` to 0.05 (= $100), while the "Why not 2%?" prose below
  still argues against a $40 cap. The table matches the code. The prose is
  stale. Not corrected unilaterally because it is a governing document.
