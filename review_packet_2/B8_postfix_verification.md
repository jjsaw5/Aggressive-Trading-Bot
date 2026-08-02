# B8 — Post-fix verification (Reviewer Rulings #2, R3)

All three gating items implemented and verified in the **rendered DOM**, not in
the source. Screens re-shot after the change.

| File | Screen |
|---|---|
| `B2_13_POSTFIX_sd_scanner_no_pick_badges.png` | 1–5DTE Scanner, 50 rows, 3 of them `engine_pick=true` |
| `B2_14_POSTFIX_sd_trade_candidates.png` | Trade Candidates |
| `B2_15_POSTFIX_core_proposals_prose.png` | Proposals — rewritten execution prose |
| `B2_16_POSTFIX_sd_candidate_detail.png` | Candidate detail — confidence removed |

The seeded scan deliberately produced engine picks (`engine_picks_marked
board=1-5dte picked=3`), so the badge had live data to render from and did not.

## R3.1 — `PICK #N` removed, all three registers

```
rows rendered            : 50
.badge.pick / .pickrow   : 0 elements
"PICK #" in rendered view: false
```

Sample row, verbatim from the DOM:

```
1  JPM  1-5dte  bullish  catalyst_continuation | time_of_day_blocked |  armed  77  2.04  $329  BLOCKED
```

Rank, symbol, no badge. `engine_pick`, `pick_rank` and `pick_reason` remain in
the API payload and the warehouse — the window should grade whether the flag
predicts anything. Captured, not displayed.

*(Note on method: an earlier check regexed `page.content()` and reported `PICK #`
still present. That was matching my own explanatory comment in the served
`<script>` block. Re-run against `innerText` of the rendered view, which is what
a user actually sees.)*

## R3.2 — standalone `confidence` removed

Detail sub-line before:

```
State armed · tradability 77/100 · confidence 72% · data quality 83% · ...
```

after:

```
State armed · tradability 77/100 · data quality 83% · quote 14496.2s (≤30s watchlist) · model sd-scoring-2026.08-v3.1
```

Not relabelled, not re-derived on screen — removed. Both of its inputs remain
visible; the multiply does not.

**Three quantities named `confidence` exist and only one was the offender.**
Verified before touching anything:

| Site | Quantity | Disposition |
|---|---|---|
| candidate detail | `ScoreCard.overall_confidence` = `score × (0.6 + 0.4 × data_quality)` | **REMOVED** |
| regime banner | `RegimeRead.confidence` — the regime engine's own read | kept, independent |
| catalysts table | `CatalystEvent.confidence` — *"reliability of the CLASSIFICATION, not a trade signal"* | kept, independent |

`news confidence` and `flow confidence` also still appear in the scorecard
component breakdown. Those are component *names* (`engine.py:113`), not the
derived scalar, and they belong there.

## R3.3 — one shared ET formatter

Eleven bare `toLocale*` calls replaced with `etDateTime()` / `etTime()` /
`etDate()`, all pinned to `America/New_York` and suffixed `ET`. Rendered proof
from the News view:

```
SPY   SPY extends move on above-average volume   mock-wire   8:49:37 PM ET   540s
```

`tests/test_dashboard_timezone.py` — **7 tests** — fails the build on any
`toLocale*(` without an explicit `timeZone`, including the exact zero-argument
forms. The one pre-existing pinned call (`timeZone: "UTC"` on the calendar month
label) is untouched and passes.

## R3.4 — colour bands served from config

```json
"display_bands": {
  "version": "sd-display-2026.08-v1",
  "pop_bad": 0.4, "pop_ok": 0.55,
  "cost_drag_good": 0.15, "cost_drag_bad": 0.3
}
```

Served on `GET /config/runtime`. The duplicated literals at the old lines 657 and
1022 are gone; `popCls()` and `dragCls()` are defined once and read the served
bands.

**The interim behaviour is now the permanent failure mode.** If the bands have
not loaded, both helpers return `""` — the neutral, non-signalling class. A
colour is a claim; a claim whose threshold we could not load must not be made.
Boot was also changed to `await loadConfig()` before the first paint, so a number
can never render one colour and then be corrected under the reader.

Nothing here reaches the scorer. `test_scoring_freeze.py`,
`test_scoring_golden.py` and `test_provider_scoring_contract.py` all pass
unchanged (53 tests across the three plus the timezone guard).

## R3.5 — execution prose rewritten to the build

Before: *"Execute is double-gated — it stays denied by default (research mode)
and only ever routes an order when live trading is explicitly armed, which it is
not."*

After: *"**There is no broker path in this build.** Execute calls the
ExecutionGuard and renders its answer — today, always a denial and the reason for
it. No code in this platform places, cancels or modifies an order, and brokerage
access is read-only. The button exists to make the gate observable and testable,
not to arm anything."*

Describes the wire that was never run, not a switch that is off.

## R3.6 / R4.4 — CI

```yaml
- name: Lint
  run: python -m ruff check .        # was: app/ scripts/ tests/

secret-scan:                          # new job
  - uses: gitleaks/gitleaks-action@v2
    with: fetch-depth: 0              # scans history, not just the tip
```

`.gitleaks.toml` extends the upstream default ruleset (generic detectors stay
on) and adds the three credential shapes this project actually handles: the
Turso database URL, the Turso auth JWT, and the UW API key. The UW rule is
anchored on the variable name rather than matching every UUID — a scanner that
cries wolf trains people to add allowlist entries, which is how a scanner stops
being read.

## Suite

```
860 passed
ruff check .   ->  All checks passed!
console errors ->  1 (the same pre-existing 404, unchanged)
```
