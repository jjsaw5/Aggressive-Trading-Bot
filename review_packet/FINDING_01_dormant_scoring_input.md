# FINDING 01 — a scoring input that is always null in production, and the trap in fixing it

**Severity: HIGH.** Raised by this packet's own acceptance checks, not by the
remediation work. It was not known when Phases 0–2 were reported complete.

## What the check found

The reviewer's Phase 1 acceptance criteria require `term_slope` populated on
every row. On the fresh sample it is populated on **0 of 6**:

```
term_slope                 0/6 populated   e.g. NA_no_data
```

## Why

`UnusualWhalesProvider.get_iv_context` (`app/providers/unusual_whales/client.py:336`)
fetches only the `iv` field from `/api/stock/{ticker}/volatility/stats`. It never
sets `term_structure_slope`, so the field is `None` on every live scan. The
plumbing all the way to the export is correct and complete — the source simply
never supplies a value.

**This corrects a claim I made.** Phase 1 item 1.3 was reported as delivered
covering "iv / iv_rank / term_slope / implied_move". Three of those four are
delivered. `term_slope` is plumbed but never populated in production.

## The part that matters more

`term_structure_slope` is **read by the frozen scorer**:

```python
# app/shortduration/scoring/components.py:137
if iv.term_structure_slope is not None and iv.term_structure_slope < -0.01:
    val *= 0.85
    parts.append("backwardated (crush risk)")
```

That is a 15% penalty on the volatility component for backwardation — an
IV-crush warning for debit buyers. **In production it has never once fired**,
because the input is always `None`.

So the obvious fix — populate `term_structure_slope` from UW's
`/api/stock/{ticker}/volatility/term-structure`, which exists and is entitled —
would **silently change composite scores mid-window** while every row continued
to report `sd-scoring-2026.07-v3`. That is precisely the corpus-splitting failure
`CAPTURE_WINDOW_PREREGISTRATION.md` §2 exists to prevent.

**Action taken: none. Deliberately.** Fixing this during the capture window
would violate the freeze. It is logged here for the reviewer to rule on.

## Second-order consequence: mock and production score differently

`app/providers/mock/provider.py:296` **does** populate `term_structure_slope`:

```python
term_structure_slope=round((s - 0.5) * 0.1, 4),
```

The test suite runs entirely on mock providers. So the backwardation branch is
exercised in every test run and never in production. Scoring behaviour differs
between the tested path and the shipped one, and no test can currently detect
that.

## Gap this exposes in the freeze guard

`tests/test_scoring_freeze.py` blocks scoring modules from *importing*
capture-only modules. It cannot detect this class of change, where a dormant
input becomes live and alters scores without any code in `scoring/` changing at
all. A provider-side edit is sufficient to break the freeze.

**Recommended follow-up (NOT taken, pending review):** a golden-file scoring
regression — fixed inputs, asserted composite outputs — so any change to what the
scorer *receives* fails a test, not only changes to what it computes. This is the
golden-file test named as optional in the packet request; it should be treated as
required.

## Suggested disposition

1. Leave `term_slope` null for the duration of the capture window. Record it in
   the export as `NA_no_data`, which is accurate.
2. Build the golden-file scoring regression first.
3. Populate `term_structure_slope` only as an explicit, dated §8 amendment that
   ends the current window and starts a new one under a new model version.
