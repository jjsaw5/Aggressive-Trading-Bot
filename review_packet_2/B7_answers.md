# B7 — The four questions

## Q1. Does the UI recommend, or does it calculate?

**It calculates in prose and recommends in pixels.**

Every sentence is a calculator. The banner on every SD tab says *"a defined-risk
tradability + cost/odds calculator — not a trade signal ... no feature has shown
net-of-cost edge ... The thesis is yours."* The score column is labelled
`tradability`, not `score`. Above it sits `⚠ UNCALIBRATED — tradability rank, not
conviction`. Abstained rows show `ABST` instead of a number. The POP paragraph
volunteers three ways its own number is optimistic. I have not found a sentence
in 2,027 lines that overstates.

Then a blue badge on the row says **`PICK #1`**.

```js
972:  const pick = c.engine_pick
        ? ` <span class="badge pick" title="${c.pick_reason}">PICK #${c.pick_rank}</span>` : "";
975:  return `<tr class="clickable ${c.engine_pick ? "pickrow" : ""}" ...
```

The row also gets a `pickrow` highlight class. So the recommendation is carried
in three registers at once — the English word "pick", an ordinal, and visual
promotion above neighbours — while the disclaimer is carried in one: text.

Users do not read a board. They scan it for the thing that looks selected. The
badge is engineered to be that thing, and the code comment concedes the intent:
*"The engine on the record: a setup it would actually take this scan."*

A calculator does not have picks. **This is the one element that has to change**,
and it is not a wording fix — the highlight class and the ordinal have to go with
it. `engine_pick` is worth keeping as *captured data* (it is exactly the kind of
thing the capture window should grade), but capturing a flag and blue-badging it
`#1` on the board are different decisions.

**Verdict: recommends. One element, but the loudest one.**

## Q2. Can a user tell what is measured and what is modeled?

**On the detail view, yes, unusually well. On the board, no.**

The candidate detail names its provenance in terms most products would not
attempt — the POP paragraph specifies Black-Scholes zero-drift risk-neutral
lognormal N(d₂) from the traded-expiry IV, then names three ways it is
optimistic, including that its hold-to-expiry horizon does not match the ±50%
exits the system actually uses. The payload carries `greeks_source`,
`cost_stress_source`, `iv_rank_source`, `pop_source`, `chain_source`,
`scoring_model_version`, `risk_policy_version`, `freshness` and
`data_quality_score`. A client literally cannot render a score without also
holding the reasons to distrust it.

On the board, that all collapses into columns of numbers. `TRADABILITY 77`,
`POP 62%`, `R:R 1.8` are typographically identical — one is a hand-weighted
rank, one is a model output with three named biases, one is arithmetic on the
plan. Nothing distinguishes them until you click through.

And one number is worse than undifferentiated: **`confidence 72%`** is not a
measurement at all. It is `score × (0.6 + 0.4 × data_quality)` — reproduced
exactly on 58 of 58 live rows (`B5_confidence_is_derived.txt`). Sitting between
`tradability` and `data quality` with a `%` on it, it reads as a third,
independent, probabilistic reading corroborating the first. There is no third
instrument.

**Verdict: excellent at depth, poor at the glance, and one element actively
misleading.**

## Q3. Would a user be surprised by anything the system does?

**Three things, in descending order.**

1. **A button labelled `Execute`.** Given a product that says it does not place
   trades, finding an Execute button is a surprise — the good kind, in that it is
   inert (`B4`: no broker call exists in any branch; the endpoint's whole effect
   is to return a denial and its reason), but a surprise nonetheless. It is
   mitigated by `(guarded)`, by the double-gate prose, and by the fact that
   pressing it visibly returns `authorized: false`.

   The residual surprise is the prose at line 438: *"only ever routes an order
   when live trading is explicitly armed, which it is not."* That describes a
   switch that is off. There is no switch — there is no broker path in this
   codebase at all. A user reading it forms a more advanced mental model of the
   system than the system deserves.

2. **Timestamps are browser-local, not ET.** Eleven `toLocaleString` calls with
   no `timeZone`. On a product whose subject is same-session expiry, "09:31"
   silently means different instants on a laptop in Denver and a laptop in
   London. Line 1562 passes `timeZone: "UTC"` explicitly, so the codebase knows
   the option exists. This is the surprise most likely to cause an actual
   mistake.

3. **Colour is a claim nobody declared.** POP goes green at ≥ 0.55 and red below
   0.40; cost-drag goes green below 0.15. Those bands exist only in the HTML —
   unserved, unversioned, undocumented, and written twice (lines 657 and 1022).
   A green number on a quantity the same screen labels UNCALIBRATED is an
   endorsement made in a stylesheet, and colour outlives text in memory.

**Not surprising, and worth saying:** the 0DTE board being empty and suspended is
correctly signposted; `NEW TRADES BLOCKED` and `ENTRY: BLOCKED` are legible; the
empty states name what is missing rather than rendering zeros; `/outcomes/
calibration` returns `null` for every statistic and `"insufficient"` for the
verdict rather than plotting an empty chart.

## Q4. Does the front end honour the product stance?

**Substantially yes — with one contradiction that it cannot honour and hold at
the same time.**

Held, and held well:

- The truth banner is on **every** SD tab, by design (*"so the framing is never
  lost"*), and it names the negative result rather than hedging around it.
- Nothing overstates in text, anywhere in 2,027 lines.
- The UNCALIBRATED derivation **fails closed** — a missing scorecard, an
  unexpected string, or a conviction-gate exception all read UNCALIBRATED. The
  `CALIBRATED` branch cannot currently render, because the gate is RED and
  degrades pessimistically.
- Absent stays absent, end to end: `null` POP, `ABST` for abstained rows,
  `null` calibration statistics, `[]` for the suspended 0DTE bucket.
- The execution double-gate is real, tested, and not bypassable from this UI.

Not held:

- **`PICK #1` is a recommendation on a screen that says it does not
  recommend.** The banner and the badge are both rendered by the same function
  call stack, roughly sixty lines apart, and they say opposite things. Whichever
  one a user believes, the other was wasted.

- **`confidence NN%` manufactures corroboration** where there is only one
  underlying quantity.

Everything else in this review is a recommendation. These two are the stance not
being honoured, and the first is not a labelling problem — the ordinal, the blue
badge and the row highlight are all doing recommendation work, and all three have
to come out together.

The gap is narrow and it is fixable in an afternoon. It is worth fixing **before
P10**, because the moment production redeploys, every screen a user reads during
the capture window is one of these.
