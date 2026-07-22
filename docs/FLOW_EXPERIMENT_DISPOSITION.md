# Flow Experiment — Disposition (auditor review, post-results)

Companion to `FLOW_EXPERIMENT_PREREGISTRATION.md`. The pre-registration is the
frozen design; this records the review outcome after results existed. Kept
separate so the frozen design stays untouched.

**Verdict:** `fail_to_reject_H0` — no demonstrated net-of-cost flow edge
(323 trades, 25 names, 2023–2026, real bid/ask k=1.0, `flow_source = proxy_eod`).

## Decisions

1. **Null accepted on the pre-registered stress requirement alone.** The 2025
   drawdown held **0 CONFIRM trades** — not thin, *empty*, and empty by
   construction: a flow-confirmed trend-long needs bullish flow and a bullish
   trend to coincide, and in a ~19% selloff flow turns bearish while the
   trend-skeleton is still long, so the two conditions become mutually exclusive
   exactly when they must co-occur. The empty cell is the evidence, not a gap in
   it. An unevaluable mandatory test is a fail. More data cannot fill a cell the
   construct forbids.

2. **Flow-as-scoring-input: RETIRED** — on structural, not merely empirical,
   grounds. A signal that definitionally empties in the one regime where the
   downside lives cannot be a scoring input for a book whose whole risk is
   downside. Do not promote flow into the composite score. **This question is
   closed.**

3. **Flow-as-veto / counter-trend: PARKED as a NEW experiment.** The failure mode
   points at its own inversion — flow *disagreed* with the still-long trend during
   the drawdown, and the trend was wrong. That is a different, untested hypothesis
   (flow as an exit/veto signal). It requires its **own** pre-registration, with:
   (a) no peeking at what we now know about April 2025, (b) the same regime caveat,
   and (c) an explicit guard that it does **not** smuggle the retired confirmer
   hypothesis back in. Parked, not a reason to keep the current construct alive.

4. **Sequencing HELD — no spend on 2021–22 flow.** The paid bear data would test
   the same construct that just demonstrated it empties under stress by design; the
   2022 bear would reproduce the empty CONFIRM arm. That is buying a more expensive
   version of the same null. Do not spend.

## Why this result is the deliverable

The machinery manufactured a false positive (a stress flag green on a
wrong-spread comparison) and the discipline caught it — by reading the census, not
the summary — **before it reached a verdict, in the same session.** This is the
third recurrence of one failure mode in the engagement (the ledger's mark-to-mid
wins; the anti-calibrated score; now a stress flag scoring the wrong spread) and
the first caught pre-action by the apparatus built to be suspicious of exactly it.
The correction only ever made the bar harder to clear — the right direction for a
fix to move a verdict.

Net: every layer tested — structures, score, forward ledger, flow overlay — has
reduced to directional beta that does not clear its own spread. The validation
system works: it tells the truth when the truth is "no edge here yet," and it
reports bad news about itself. The counter-trend/exit reframing (§3 above) is the
one live thread worth a future, freshly pre-registered look; everything else is
correctly closed.
