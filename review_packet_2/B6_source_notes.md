# B6 — Source notes

`app/web/dashboard.html` — 2,027 lines, served by `app/api/routes/dashboard.py:17`.

## Structure

Vanilla JS. No framework, no build, no dependencies, no npm. Rendering is
template literals returning HTML strings; state is module-scope variables; events
are one delegated click handler dispatching on `data-act`.

For a two-thousand-line single-file app this is legible and consistent. The
routing (`setView`, line 1739) is clean and every view is hash-linkable.

## The structural consequence that matters for this review

**There is no component boundary, so there is no place for a claim to live
once.** Every caveat is a string at its point of use. Three concrete symptoms:

1. **The POP colour threshold is written twice** — line 657 and line 1022, the
   identical `< 0.4 ? "neg" : < 0.55 ? "warn" : "pos"` expression, in two views,
   unlinked. They must agree; nothing makes them.
2. **`sdTruthBanner()` is the one claim that IS factored into a function** — and
   it is, correspondingly, the one claim applied consistently across every SD
   tab. This is the counter-example that proves the point: factoring the claim is
   what made it reliable.
3. **Timestamp formatting is repeated eleven times** with no `timeZone` option,
   and once (line 1562) with `timeZone: "UTC"`. A shared formatter would have
   made the inconsistency impossible.

## Where the caveats come from

Encouragingly, most are **relayed, not invented**. `app/shortduration/scoring/
engine.py` builds the `UNCALIBRATED` stamp, the `conviction_note`, the
`abstain_reason` and the summary line server-side; the client renders what it is
given. The client's own contributions are the layout, the colour thresholds, and
the `PICK #N` badge — and those three are exactly where the display-honesty
problems are.

That is a useful diagnostic: **the honest parts are the parts the server
computed; the problematic parts are the parts the browser decided.**

## Fails-closed behaviours worth crediting

```js
1097:  const uncal = !sc || sc.conviction_status !== "CALIBRATED";
```
A missing scorecard reads UNCALIBRATED. Anything other than the exact string
`"CALIBRATED"` reads UNCALIBRATED. This is the correct default and mirrors
`engine.py`, where a conviction-gate exception degrades to `"UNCALIBRATED"`
rather than to the optimistic value.

```js
979:  c.scorecard.abstained ? '<span class="neg">ABST</span>' : `${(c.score*100).toFixed(0)}`
```
An abstained candidate shows `ABST`, not a number. The UI does not manufacture a
plausible-looking score for a row the engine refused to rank.

Empty states say what is absent and why — `"No resolved decisions yet"`,
`"No candidates. Run a scan."`, `"No level set — this position can only be
managed on P&L."` — rather than rendering a zero or a blank chart.

## Recommendations, in priority order

1. **Remove or relabel `PICK #N`** (B3, B7 Q1) — the one element that
   contradicts the product stance.
2. **Drop the separate `confidence %`** or render it as `score × data quality`
   (B5) — the one element that manufactures corroboration.
3. **Pin `timeZone: "America/New_York"` and label it `ET`**, via one shared
   formatter (B5) — a correctness hazard on a 0DTE product, not polish.
4. **Serve the colour thresholds from config** and de-duplicate lines 657/1022.
5. **Soften "only ever routes an order when live trading is explicitly armed"**
   to describe the actual state: no broker path exists in this build (B4).
6. **Extend CI's ruff scope** past `app/ scripts/ tests/` (A_test_suite.txt).
