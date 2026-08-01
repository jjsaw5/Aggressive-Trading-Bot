# B3 — Claims inventory

Every user-facing string that makes, qualifies, or implies a claim about
predictive power. Line numbers are `app/web/dashboard.html`.

## The banner — shown on every short-duration tab (line 904–912)

> **What this is:** a defined-risk **tradability + cost/odds** calculator — not a trade signal.
>
> Across a full-cycle real-mark backtest, a pre-registered flow experiment, and out-of-sample feature validation (pricing *and* flow), **no feature has shown net-of-cost edge** — every layer reduces to directional beta + spread tax. These ranks show you the odds (POP), the spread tax (cost-drag), the regime and what has to happen. **The thesis is yours.**

Code comment above it: *"Shown on every short-duration tab so the framing is
never lost."* That is the correct instinct and it is honoured — the banner is
not a one-time splash a user dismisses and never sees again.

**Assessment: this is the strongest honesty artifact in the product.** It names
the negative result, cites where it lives, and hands the thesis back to the
human. Nothing else in the UI contradicts it in words.

## Score labelling (1105–1112)

| Element | Text | Verdict |
|---|---|---|
| 1106 | `⚠ UNCALIBRATED — tradability rank, not conviction` | honest |
| 1111 | column labelled `tradability`, rendered `NN/100` | honest — the word "score" is avoided |
| 1097 | `uncal = !sc \|\| sc.conviction_status !== "CALIBRATED"` | **fails closed** — a missing scorecard reads as UNCALIBRATED, not as calibrated |
| 1104 | abstention: `The tradability number below must not be read.` | honest, and unusually direct |
| 979 | abstained rows render `ABST`, not a number | honest — no plausible-looking number is manufactured |

Server-side, the stamp is generated the same way
(`app/shortduration/scoring/engine.py:166`): `stamp = "UNCALIBRATED" + ("" if
pop_available else " · POP unknown")`, and the summary line ends *"Not calibrated
conviction."* The client is not inventing the caveat; it is relaying it.

The `CALIBRATED` branch exists but **cannot currently render** — it is derived
from `conviction_gate().green`, the gate is RED, and a gate exception degrades
to `"UNCALIBRATED"` rather than to the optimistic value.

## POP labelling (1035, 1053)

Row-level: `Chance of profit (implied, UNCALIBRATED)`.

Detail-level, verbatim — the single most careful paragraph in the UI:

> **Chance of profit provenance:** Black-Scholes **zero-drift** risk-neutral lognormal (closed-form N(d₂)) from the **IV of the traded expiry** and the structure break-even. Three caveats: (1) the break-even **nets the debit at mid — it excludes the entry spread you cross**, so it's optimistic by ~half the spread; (2) it's a **hold-to-expiry** probability of finishing past break-even, so it does not model the early ±50% take-profit/stop exits the system actually uses; (3) it's a risk-neutral pricing quantity and **UNCALIBRATED** — not checked against realized win rates. Arming depends on POP being computable, but treat the number as the market's implied odds, not a validated forecast.

It names its own direction of bias, its horizon mismatch with the system's own
exit policy, and its epistemic status. This is the standard the rest of the UI
should be held to.

## Other qualified claims

| Line | Text | Verdict |
|---|---|---|
| 681 | `UNCALIBRATED — recorded to be graded, not a recommendation.` | honest |
| 1348 | `Book A measures raw signal edge; Book B is what a $2,000 account could actually take. The gap is the account's opportunity cost, not a signal failure.` | honest — pre-empts a specific misreading |
| 605 | `No invalidation level recorded — this position can only be managed on P&L.` | honest about a capability gap |
| 565 | `No degeneracy warnings — a gradeable, multi-regime, loss-bearing sample.` | honest — states what makes a sample gradeable |

## THE PROBLEM: `PICK #N` (line 972)

```js
const pick = c.engine_pick
  ? ` <span class="badge pick" title="${c.pick_reason}">PICK #${c.pick_rank}</span>`
  : "";
```
Plus `<tr class="clickable pickrow">` — the whole row gets a highlight class.

**A blue badge reading `PICK #1` is a recommendation.** Not a hedged one, not a
ranked one — the English word "pick", numbered, visually promoted above its
neighbours. The code comment says *"The engine on the record: a setup it would
actually take this scan"* — which concedes the point: the UI is saying the
engine would take this trade.

This sits on the same screen as a banner stating no feature has shown
net-of-cost edge. **The banner and the badge cannot both be true readings of the
same product.** The badge is the single element that most directly contradicts
the product stance, and it does so in the strongest available register: colour,
hierarchy, and an imperative noun.

See B7 Q1.

## Also worth flagging

**`confidence NN%` (line 1112)** — presented as a peer of `tradability` and
`data quality`. It is neither independent nor a probability: it is
`score × (0.6 + 0.4 × data_quality)`, reproduced exactly on 58/58 live rows in
`B5_confidence_is_derived.txt`. The `%` sign on a rank-derived scalar invites
reading it as a likelihood.

**`SWING · Nd` pill and `NEW TRADES BLOCKED` badge** — both honest; they surface
constraints rather than opportunities.
