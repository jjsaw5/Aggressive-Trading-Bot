# Trade evaluator — the rubric

**Version:** `trade-eval-2026.08-v1` (`settings.trade_eval_version`)
**Surface:** dashboard → Evaluate → Trade Evaluator · `POST /research/evaluate`
**Status:** UNCALIBRATED. Nothing here has cleared out-of-sample validation.

This document exists because a rubric that lives only in code is not a rubric —
it is a number with no stated meaning, and CLAUDE.md was written after exactly
that failure mode. Every threshold below is checkable against
`app/engine/trade_evaluator.py` and `tests/test_trade_evaluator.py`.

---

## 1. What this is, and why it is not the scanner

The scanner asks **"what should I look at?"** and answers under this account's
risk limits: $100 per trade, $300 aggregate heat, 4 concurrent positions.

The evaluator asks **"here is a trade I am considering — what is wrong with
it?"** and deliberately ignores all of that. You can hand it a $1,100 spread and
it will grade the spread, not your account.

This is a closer fit to the product stance than the scanner is.
`docs/PRODUCT_STANCE.md` says the tool is not a picker and that *"the thesis is
the human's; the tool makes it cheaper to evaluate."* That is this feature's job
description. The scanner generates theses. This evaluates yours.

### Where the account limits are removed

Two places, both deliberate:

| Leak | Where | How it is removed |
|---|---|---|
| per-trade risk cap | `strategy_selector.py:50` turns `RiskPolicy` into `max_debit_usd` | not used; the alternative is selected with `max_debit_usd=inf` |
| affordability cap | `OptionLiquidityConfig.max_mid_price = 25.0`, commented *"keeps 1-lot affordable for small acct"* | raised to 100,000 in `_EVAL_LIQ` |

The second is the subtle one: it is a **budget constraint wearing a liquidity
costume**. The genuine liquidity floors (open interest, volume, spread) stay,
because a structure nobody trades is a bad suggestion at any price.
`tests/test_trade_evaluator.py::test_the_alternative_is_selected_without_a_budget_cap`
pins this, and asserts its own fixture actually contains legs the cap would have
excluded — a budget-blindness test that never sees an expensive contract proves
nothing.

---

## 2. What the grade IS

**A grade of construction.** Six dimensions, each measuring something that is
arithmetic, model-implied at the market's own quoted IV, or directly observable.
None of them requires calibration to be true.

**A trade can grade well and lose.** Direction is not assessed here at all. What
a bad grade means is that the arithmetic, the odds at current implied vol, or the
execution cost are against you *before* direction is even considered.

## 3. What the grade is NOT

- **Not a prediction of profit.** The conviction gate is RED. No feature behind
  any of this has passed out-of-sample validation, and the registries are empty.
- **Not a directional opinion.** Nothing scores whether the underlying will go
  up. That is your thesis and the tool does not have one.
- **Not affordability advice.** The suggested alternative is frequently far more
  expensive than the structure you proposed — in testing, a $0.67 spread was
  contrasted against an $11.34 one. That is the feature working as specified:
  you asked for the trade to be judged, not the position size.
- **Not a substitute for the risk policy.** `docs/RISK_POLICY.md` still binds
  what you actually trade. The evaluator is silent about it by design.

---

## 4. The six dimensions

Each returns `strong` / `acceptable` / `weak` / `fail`, or `not_assessed` with a
sentinel (`NA_no_data`, `NA_not_implemented`). Scores are 1.0 / 0.7 / 0.4 / 0.1.

### 4.1 Cost & structure — weight 0.20

**Spreads** are measured on *cost drag*: the debit as a fraction of the width.

| Drag | Verdict |
|---|---|
| ≤ 35% | strong |
| ≤ 50% | acceptable |
| ≤ 65% | weak |
| > 65% | fail |

Pay 65% of the width and the underlying must travel most of the way to the short
strike just to break even, for a best case of 0.54:1.

**Single long legs** have no width, so drag is meaningless. The measure is the
*extrinsic* fraction of the premium — the part that decays to zero if price does
nothing (strong ≤ 40%, acceptable ≤ 65%, weak ≤ 85%).

### 4.2 Probability (modelled) — weight 0.25

Black-Scholes P(profit at expiry) at the long leg's own implied volatility,
against the structure's single break-even. Thresholds come from
`settings.display_pop_ok` (0.55) and `display_pop_bad` (0.40); below 0.25 fails.

Its value is not that it is right — it is that it is **independent of your
thesis**. A structure the market prices as a 1-in-8 shot is that whether or not
your direction call is good.

**Unmodellable odds score `not_assessed`, never a default.** A probability we
could not compute is not evidence of a good one — the same rule Amendment 2 put
into contract selection.

### 4.3 Liquidity & execution — weight 0.25

The dimension that needs no calibration at all, and the one that quietly kills
short-dated retail trades.

- **Widest leg spread** as a fraction of mid: strong ≤ 5%, acceptable ≤ 10%,
  weak ≤ 20%.
- **Round-trip spread tax**: buy the ask, sell the bid, in and out — as a
  fraction of defined max loss. Bands from `settings.display_cost_drag_good`
  (15%) and `display_cost_drag_bad` (30%).
- Minimum open interest and volume across the legs are reported. Thin OI widens
  the **exit**, not just the entry.

The verdict is the **worse** of the quoted spread and the round-trip tax.

### 4.4 IV context — weight 0.10

Polarity is easy to get backwards and matters: every structure the evaluator
handles is a **debit**, so you are **long vol**. High IV rank is against you —
you pay up front and an IV drop hurts even when direction is right.

IV rank ≤ 30% strong, ≤ 55% acceptable, ≤ 75% weak, above that fail. IV/HV is
reported alongside.

### 4.5 Timing & events — weight 0.10

Earnings inside the holding window drops the verdict to `weak` — not a
disqualification, because trading an event is a legitimate choice, but it must be
a *choice* rather than a surprise.

**An unknown earnings date scores `acceptable`, not `strong`.** Absence of a date
from the calendar feed is not evidence of absence of an event.

0DTE is flagged OBSERVATION ONLY, consistent with Amendment 3.

### 4.6 Versus the best available — weight 0.10

Runs the platform's own `select_vertical_spread` / `select_long_contract`
**unconstrained, pinned to the same expiry**, and reports the probability gap.
Scored on that gap: ≤ 2% strong, ≤ 8% acceptable, ≤ 15% weak, above that fail.

Pinning to the resolved expiry is deliberate — suggesting a different expiry
would answer a question you did not ask.

When you supply no strikes there is nothing to contrast, and the dimension
reports `NA_not_implemented`. **"No gap" and "no comparison" are different
statements** and the report does not conflate them.

---

## 5. How the composite works

Weighted mean **over assessed dimensions only**, renormalized.

A dimension with no data contributes **nothing**, not zero. A missing feed that
silently contributed 0.0 would be indistinguishable from a measured failure —
that is the "absent stays absent" rule (CLAUDE.md §4), and it is the single most
important property of the composite.

| Composite | Grade |
|---|---|
| ≥ 0.80 | A |
| ≥ 0.65 | B |
| ≥ 0.50 | C |
| ≥ 0.35 | D |
| else | F |

**Any single `fail` caps the grade at D.** A structure that fails one hard bar is
not rescued by scoring well elsewhere, and a plain average would let it be. The
far-OTM lottery ticket is the case this exists for: it scores **strong** on cost
drag (13% of width, 6.5:1 reward-to-risk) and **fails** on probability at 12%.
That combination is exactly the trap the pre-Amendment-2 fit function fell into,
and averaging would have returned a B.

**The report always states how many dimensions it saw.** A B over four of six is
not the same claim as a B over six.

**Nothing assessed yields no grade, not an F.** An F is a measurement.

---

## 6. Horizon resolution

Accepts `0d` / `3d` / `2w` / `45d` / `6m`, or an ISO date. Snapped to the nearest
**listed** expiration and echoed back with a note.

`3d` on a Thursday and `3d` on a Monday are different contracts, so the expiry
actually used is reported rather than implied. An unreadable horizon returns a
gap with a reason — guessing a default would silently evaluate a *different*
trade from the one asked about.

---

## 7. Persistence, and why it is quarantined

Evaluations are written to **`trade_evaluations`**, their own table, and to
nothing else. Never `decision_snapshots`.

The reason is not tidiness. A user can evaluate the same bad idea forty times.
Counting those as decisions would move the base rate the conviction gate is
measured against, and that gate is the only thing standing between UNCALIBRATED
and a claim. `app/analytics/calibration.py` does not read this table.

This repository has polluted the capture corpus **twice** from code that wrote as
a side effect of being called, and an evaluator — invoked ad hoc, on arbitrary
tickers, repeatedly — is exactly the shape that causes a third. So:

- `app/engine/trade_evaluator.py` and `app/research/evaluate.py` do not import
  the persistence layer at all;
- the route is the only writer, behind `settings.trade_eval_persist`;
- `tests/test_trade_evaluator_isolation.py` enforces both statically (AST) and
  behaviourally (every repository writer monkeypatched, then a real evaluation
  run, asserting none fires).

Note that `app/research/symbol.py` — the neighbouring symbol report — **does**
call `run_detection`, which persists unconditionally. The evaluation path
deliberately does not reuse that fan-out, and a test pins the separation because
the two modules look similar enough that merging them would be a silent mistake.

---

## 8. Relationship to the frozen scoring model

`trade_eval_version` is deliberately **not** `scoring_model_version`.

The evaluator calls the frozen scorer's neighbours read-only, but it produces a
different artifact answering a different question. Borrowing the frozen version
would make a change to the evaluator look like a change to the shipped scoring
model — precisely the confusion the freeze exists to prevent.

**Bumping `trade_eval_version` does not end the capture window.**
`tests/test_scoring_freeze.py`, `tests/test_scoring_golden.py` and
`tests/test_provider_scoring_contract.py` remain the authority on that, and
nothing in this feature touches a guarded path.

---

## 9. Known limits

- **Debit structures only.** Credit and undefined-risk structures need a
  different risk model; returning a confident grade for one the platform cannot
  price would be worse than refusing.
- **Greeks are Black-Scholes** (`greeks_source`), because no provider supplies
  them. Labelled everywhere they appear.
- **Probability is modelled, not calibrated.** It uses the market's implied vol,
  which is an assumption about the future, not a measurement of it.
- **Marks are as-of the chain fetch.** The evaluator does not yet display quote
  age; the same staleness question raised about the scan boards applies here.
- **The alternative is only as good as the selector.** It inherits Amendment 2's
  behaviour, including the POP floor of 0.25 — so on a chain where nothing clears
  that floor, the contrast dimension reports a gap rather than a suggestion.
