# PROPOSAL — Amendment 2: contract selection must price probability, not only payoff

**STATUS: PROPOSAL. NOT APPROVED. NO CODE HAS BEEN CHANGED.**

This document exists to be approved or rejected before anything is edited. It is
written under `docs/PROPOSED_*` deliberately: `CAPTURE_WINDOW_PREREGISTRATION.md`
§8 is amended only by a dated entry, and a frozen design must not be touched by a
proposal that has not been accepted (governance §7).

| | |
|---|---|
| Raised | 2026-08-03, from a user observation |
| Affects | `app/engine/contract_selection.py`, `app/shortduration/contracts.py` |
| Model impact | **Yes** — `sd-scoring-2026.08-v3.1` → `sd-scoring-2026.08-v4.0` |
| Window impact | **Ends the pre-window state; the capture window restarts from zero** |
| Decision owner | Justin |

---

## 1. The observation

> "Why do we not see the app surface plays like this? Where it's reasonable to
> enter 87 dollars and short duration on something like SPY?"

A SPY 759/761 call debit spread, 3 DTE, $0.87 debit — $87 of defined risk, inside
the $98.15 per-trade cap. The app never offers anything resembling it. For SPY it
offered a 790/815 spread, 28 DTE, $0.905 debit.

## 2. What the app itself recorded about its own pick

From `short_duration_candidates`, 2026-08-03 18:40 UTC, verbatim:

```
SPY  trend_continuation  K=[790.0, 815.0]  exp=2026-08-31  net=0.905
     POP = 0.1044        R:R = 26.62
     what_has_to_happen: "SPY must rise 4.4% to $790.90 by expiry (28d)
                          just to break even."
```

A 10.4% probability of profit. A 4.4% SPY move in 28 days required merely to
break even. The system computed that number, stored it, displayed it on the
board — and did not use it when choosing the contract.

## 3. This is systemic, not one bad row

All structures priced on 2026-08-03 (n = 64):

| POP band | count | share |
|---|---|---|
| < 15% | 15 | 23% |
| 15–25% | 14 | 22% |
| 25–40% | 23 | 36% |
| 40–55% | 8 | 12% |
| ≥ 55% | 4 | 6% |

- median POP **0.287**
- median R:R **7.79 : 1**
- 22 of 53 structures at **R:R ≥ 10 : 1**

**45% of everything the scanner produced has a probability of profit below 25%.**
A board whose median offering is a 7.8:1 payoff at 29% odds is a board of lottery
tickets. That is not a defined-risk cost-and-probability calculator; it is a
long-shot generator with a probability column attached for decoration.

## 4. Root cause — three independent mechanisms

### 4.1 The fit function optimises payoff and ignores probability

`app/engine/contract_selection.py:232`:

```python
fit = min(1.0, rr / 2.0) * 0.7 + min(1.0, width / long_leg.strike * 20) * 0.3
```

70% reward-to-risk, 30% width. **Probability of profit does not appear.** Both
terms increase as the structure moves further out of the money: R:R rises because
the debit falls, and width rises because the short leg is pushed further away.
The function's maximum is, by construction, the cheapest far-OTM spread the chain
allows.

Scored against the user's own screen (SPY spot 758.42, 8/6 expiry):

| spread | debit | width | R:R | fit |
|---|---|---|---|---|
| **790/815 — what the app chose** | 0.905 | 25 | 26.6 : 1 | **0.861** |
| 761/763 | 0.59 | 2 | 2.39 : 1 | 0.716 |
| 760/762 | 0.73 | 2 | 1.74 : 1 | 0.625 |
| **759/761 — what the user chose** | 0.87 | 2 | 1.30 : 1 | **0.470** |

The human's trade ranks **last of four**. The function would also rank the three
$2-wide spreads backwards against each other, preferring the furthest OTM.

### 4.2 The short leg is unconstrained

The long leg must sit inside the delta band (`min_delta` 0.30–0.35,
`max_delta` 0.60–0.65 depending on track). The short leg is checked only for
liquidity — no delta floor, no width ceiling. So the optimiser slides it as far
out as the chain permits, which is precisely the move that maximises both terms
of the fit score. Nothing opposes it.

### 4.3 Every trend signal is forced weeks out

```python
_SWING_STRATEGIES = {ShortDurationStrategy.TREND_CONTINUATION}
swing_min_dte = 21;  swing_max_dte = 45
```

SPY's signal is `trend_continuation`, so a 3-DTE expression is **structurally
unreachable** — not merely unranked. Only ORB, VWAP-continuation and
catalyst-continuation run the 1–5 DTE window.

This was a deliberate fix (item 1.11: a daily-trend thesis cannot resolve inside
a 4-DTE contract). It has over-corrected: the rule is applied to the *strategy*
rather than to the *thesis horizon*, so an index trend read that could legitimately
be expressed over three days never is.

## 5. Why this is a frozen-model change — and why CI would NOT have caught it

`app/shortduration/scoring/components.py:181`:

```python
def risk_reward(ctx, direction, plan=None) -> ScoreComponent:
    rr = plan.risk.reward_to_risk
```

The scorer reads reward-to-risk **off the selected plan**. Change which structure
is selected and you change a scored component, hence the composite, hence the
shipped model — with no edit inside `app/shortduration/scoring/`.

**`app/engine/contract_selection.py` is not in the freeze guard's `GUARDED`
regex.** The path check in `.github/workflows/ci.yml:118` covers
`app/shortduration/scoring/`, `app/shortduration/strategies/`, the two provider
files and `app/engine/iv_context.py`. It does not cover contract selection.

This is FINDING_01 recurring in a new file: *the freeze is about behaviour, not
about which files you edited* (CLAUDE.md §2). A change here would ship a
different model and pass every control.

**Recommendation, independent of whether this proposal is accepted:** add
`app/engine/contract_selection.py` and `app/shortduration/contracts.py` to
`GUARDED`. That is a control fix, not a model change, and should land regardless.

## 6. Proposed changes

### C1 — Probability enters the fit function

```python
fit = 0.45 * pop + 0.35 * min(1.0, rr / 2.0) + 0.20 * min(1.0, width / strike * 20)
```

POP becomes the largest single term. Payoff still counts; it stops being able to
outvote the odds on its own.

### C2 — Hard POP floor

Reject any structure below **POP 0.25**. Not down-rank — reject. A structure the
system believes will lose three times in four is not a defined-risk candidate,
and ranking it merely relocates the decision to the reader.

### C3 — Short-leg delta floor

Require short-leg |delta| ≥ **0.15**. This caps width at something the market
prices as plausible instead of "as far as liquidity allows", and directly closes
4.2.

### C4 — Horizon by thesis, not by strategy label

Allow `trend_continuation` to express at 1–5 DTE when the thesis' own
`distance_to_invalidation_pct` and expected-move arithmetic support resolution
inside that window; keep the 21–45 DTE default otherwise. Retain the existing
Medium-Duration routing for the ones that stay long-dated.

C4 is the largest change and the least certain. **It can be deferred** without
weakening C1–C3.

### Simulated effect on the live board (18:40 UTC batch)

| sym | POP | R:R | width | fit now | fit proposed | POP ≥ 25% floor |
|---|---|---|---|---|---|---|
| AAL | 0.368 | 2.2 | 2 | 1.000 | 0.715 | PASS |
| IWM | 0.177 | 11.2 | 12 | 0.934 | 0.586 | **REJECT** |
| SPY | 0.104 | 26.6 | 25 | 0.890 | 0.524 | **REJECT** |
| BAC | 0.331 | 1.9 | 2 | 0.870 | 0.615 | PASS |
| MSFT | 0.101 | 13.0 | 10 | 0.817 | 0.473 | **REJECT** |

Three of five current structures fail the floor. The ordering inverts: the two
survivors are the two the fit function currently ranks 1st and 4th, and the
long-shots drop out entirely.

**Honest note on this simulation.** It re-scores the structures the *current*
selector chose. It does not show what a fixed selector would pick instead, because
that requires re-running selection against the full chain. Expect the real effect
to be better than this table — the near-ATM structures the current rule never
surfaces are absent from it.

## 7. What accepting this costs

1. **`scoring_model_version` → `sd-scoring-2026.08-v4.0`.** A minor bump would
   understate it: this changes which instrument a signal is expressed in, not a
   coefficient.
2. **A dated Amendment 2 under `CAPTURE_WINDOW_PREREGISTRATION.md` §8.**
3. **`tests/golden/scoring_v3.json` regenerated**, delta documented. The golden
   file scores fixed `IVContext` fixtures, so it may not move on its own —
   see the P1 note; the provider-contract and structure tests are where the
   change will show. `tests/test_structures.py` covers `select_vertical_spread`
   and will need updating.
4. **The capture window restarts.** Zero signals have been captured, so nothing
   is lost — but if production is redeployed before this lands, anything captured
   in between is under v3.1 and cannot pool with v4.0 data.

## 8. Alternatives considered

**A. Do nothing; capture the window as-is.** Rejected. The window's purpose is to
learn whether the ranking predicts outcomes. A corpus of sub-25%-POP long shots
will produce a low hit rate that tells you about the *contract selector*, not the
*signal*, and the two will be unseparable after the fact.

**B. Fix the display only — surface POP more prominently, leave selection alone.**
Rejected. The board already prints POP and `what_has_to_happen` in plain English
("SPY must rise 4.4% … just to break even"). The information is present and the
selector still chooses against it. Making the warning louder does not change what
gets offered.

**C. C2 alone (POP floor, no re-weighting).** Viable as a minimum. It removes the
worst rows without re-ordering the survivors. Cheaper to justify, less effective:
it truncates the distribution rather than changing what the optimiser is aiming at.

**D. Full proposal, C1–C4.** Recommended.

## 9. Decision required

- [ ] **Approve C1–C3**, defer C4 — *recommended if you want the window to open soon*
- [ ] **Approve C1–C4** — the complete fix, more work, larger surface
- [ ] **Approve C2 only** — minimum viable
- [ ] **Reject** — capture the window under v3.1 as-is
- [ ] **Approve the guard fix (§5) regardless** — *recommended in every case*

Nothing proceeds until this is signed off. On approval I will produce the §8
amendment, the version bump, the code, the regenerated golden file and the test
updates as a single reviewable commit.
