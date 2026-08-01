# Session Log

Required by `CLAUDE.md` §3. One entry per working session: what changed, why,
PRs, decisions, and an explicit **DEVIATIONS** section — `None` written out, so
its absence is a claim rather than an oversight.

---

## Entry 0 — 2026-08-01 — RECONSTRUCTION, and the root cause of this file's absence

**This entry is retrospective.** It covers Phases 0–2 and the reviewer rulings
that followed, reconstructed from commits, PRs and the audit packet. It is not a
contemporaneous record and should not be read as one. Entries from this point
forward are per-session.

### Root cause: why no session log existed

The reviewer (Ruling 3) asked which of three causes applied. The answer is
determinate and is the first one:

```
$ git log --all --oneline -- CLAUDE.md .claude/CLAUDE.md
(no output — zero commits in any branch have ever touched it)

$ ls .claude/
(no such directory)

$ grep -rl "SESSION_LOG" --include=*.md --include=*.py .
(no match anywhere in the repository)
```

**`CLAUDE.md` has never existed in this repository.** Not stale, not
mis-located, not ignored — never committed, in any branch, at any point. The
governance block requiring a session log lived only in session context, which is
ephemeral and invisible to the repository. No session could have read it from the
checkout, because there was nothing to read.

This is a governance escape rather than a compliance failure: a standing
requirement with no durable home is not discoverable, not auditable, and cannot
survive a new session, a new contributor, or a fresh clone. The same absence
explains why the never-auto-trade rule, the secret-handling discipline and the
freeze rules were also uncommitted — every one of them was being carried in
conversation.

**Remediation, this session:** `CLAUDE.md` committed at repository root with the
governance block, the freeze rules, the honesty rules, the risk limits and the
session-log requirement. `docs/audit/SESSION_LOG.md` created (this file).

### What changed, Phases 0–2

| PR | Commits | Substance |
|---|---|---|
| #48 | `dd65f06`, `37a129b`, `104ce41`, `7b8fb06` | Signal-audit export; Phase 0 capture gates + execution gate + spot persistence; Phase 1 market context; Phase 2 intraday grading |
| #49 | `3e292b2`, `46e3738` | Audit packet; Ruling 1 — FINDING_01 closure, v3 → v3.1 |

**Phase 0.** Earnings-before-expiry became a rejection rather than advisory
prose (an AAPL call spread had been picked #1 the day before earnings, with the
conflict detected and written into text that gated nothing). 0DTE suspended.
The conviction gate now blocks execution and fails closed when unevaluable.
`entry_spot` made nullable and `or 0.0` fallbacks removed — all 67 audited
scanner rows had reported a spot price of zero.

**Phase 1.** Per-leg NBBO, depth, IV term structure, cost drag, earnings
distance, realized vol and VRP, producing-build SHA, and a composite regime tag,
frozen onto every decision. Most of it was already computed on every scan and
discarded before reaching the warehouse.

**Phase 2.** `IntradayOptionsProvider` on UW's
`/api/option-contract/{id}/intraday`, then minute-resolution managed replay,
measured exit price and timestamp, MFE/MAE, cost stress, and modeled-vs-actual
fill comparison. All 38 audited 0DTE signals had resolved `expiry` because a
one-point-per-day series cannot contain a same-session exit.

### Decisions taken

1. **No paid data vendor, no Robinhood credentials.** A $199/month feed and
   broker credentials on the Docker host were recommended, then withdrawn when
   the user asked for the alternatives to be checked again. UW's
   `/api/option-contract/{id}/intraday` was already entitled on the existing
   subscription and had been in the vendor's OpenAPI spec throughout. The prior
   recommendation rested on reading our own module docstring ("there is no
   intraday" — true of the `/historic` endpoint we had implemented) as a
   statement about the vendor.
2. **Intraday grades never overwrite daily ones.** Written as
   `managed_policy_intraday` alongside `managed_policy`, so the daily
   approximation's distortion stays measurable.
3. **`UW_INTRADAY_ENABLED` separate from `UW_HISTORIC_ENABLED`.** Different
   endpoints, independently entitled; a shared flag would silently disable one
   with the other.
4. **FINDING_01 fixed before the window rather than deferred** (reviewer Ruling
   1). Zero signals captured meant no corpus to split.
5. **DTE 0 excluded from the term-structure front leg.** The expiration-day ATM
   IV solve is unstable (SPY: 0.24 at dte=0 against 0.08 at dte=3); anchoring
   there would manufacture backwardation on every 0DTE-listed name.

### DEVIATIONS

**Not None.** Five, all self-reported before the reviewer asked:

1. **Item 1.3 reported complete; it was 3 of 4.** `term_slope` was plumbed
   end-to-end but never populated by any live provider. Surfaced by running the
   reviewer's own acceptance criteria against a fresh export. Became FINDING_01;
   fixed under Ruling 1 in `46e3738`.
2. **Item 1.4 diverges from specification.** Delivered as on-demand fetch, not a
   polling loop. There is no cadence and no stored mark series. Accepted by
   Ruling 2 for 1–5DTE with mandatory mark-quality columns; rejected for 0DTE,
   which now carries a quantitative re-enable bar (≥80% RTH coverage, max gap
   ≤5 minutes).
3. **Item 1.11 diverges from specification.** Built as a per-signal vol×tape
   tag rather than the specified daily VIX/SPX regime table. Rejected by Ruling
   2; the daily table is to be built, with the per-signal tag retained as a
   supplementary column.
4. **Ruling 1 step 2 could not be satisfied as written.** The ruling predicted
   the golden file would break on the term_slope fix. It does not and
   structurally cannot: it scores fixed `IVContext` fixtures, so a provider
   change cannot move its numbers. The equivalent proof came from the
   provider-contract test (8 of 9 failing pre-fix, 9 of 9 passing post-fix) and
   from the freeze guard independently catching the version bump. Reported in
   the PR body and commit message rather than worked around.
5. **84 mock-provider rows were written to the live warehouse.** Verifying the
   Phase 1 wiring end-to-end meant running a real detection against mock
   providers, and `run_detection` persists unconditionally. Identified via the
   `chain_source` provenance field added in the same phase, and purged with
   `scripts/purge_mock_signals.py` after user approval (84 snapshots, 84
   candidates, 248 transitions; zero collateral). One residue could not be
   reverted: `retire_engine_picks` cleared the `engine_pick` flag on 4
   candidates from earlier real scans.

### State at entry close

- **Production has NOT been redeployed.** Still running `7afa098`. No deployed
  signal has ever carried capture gates, market context, or intraday grading.
- **Capture window has NOT started.** Zero signals captured.
- Model `sd-scoring-2026.08-v3.1`; freeze point `80eb42c`.
- 812 tests passing, ruff clean.
- **Outstanding:** rotate the UW API key and both Turso tokens — all three were
  exposed in a session transcript and remain unrotated.

---

## Entry 1 — 2026-08-01 — reviewer rulings closed; pre-flight P1–P8

Contemporaneous. Covers the reviewer's rulings on the audit packet of 2026-08-01
and pre-flight items P1 through P8. Entry 0 above is the retrospective covering
everything before this point.

### What changed, and why

| PR | Merge | Substance |
|---|---|---|
| #49 | `80eb42c` | Ruling 1 — FINDING_01 closed; `sd-scoring-2026.07-v3` → `sd-scoring-2026.08-v3.1` |
| #50 | `1ccfc16` | P3 CI + freeze path guard; P8 `CLAUDE.md` + this file; `docs/FREEZE_POINT.md` |
| #51 | `34d0bad` | P6 daily regime table; P7 mark quality + 0DTE bar; P5 data dictionary |

**Ruling 1 (P1/P2).** FINDING_01 fixed *before* the window rather than deferred.
The team's instinct — leave the dormant input alone — was right for mid-window
and wrong for now: zero signals captured means no corpus to split, which made
this the one free moment to close the gap between the tested model and the
shipped one. Ordered as mandated: controls first, then the fix.

`get_iv_context` now populates `term_structure_slope` from UW
`/volatility/term-structure`. DTE 0 is excluded from the front leg deliberately —
the expiration-day ATM IV solve is unstable (SPY printed 0.24 at dte=0 against
0.08 at dte=3) and anchoring there would manufacture backwardation on every
0DTE-listed name. Version bumped to v3.1 with weights, components and thresholds
byte-identical: the version records that the SHIPPED model changed, because
`components.py:137` had applied a 0.85 backwardation penalty since v3 that no
live provider could ever trigger. Golden reference measures it at 50.6 against a
52.1 baseline. Pre-registration §8 Amendment 1 recorded, dated, sections 3–7
untouched.

**Ruling 2 (P6/P7).** The per-signal vol×tape tag was rejected as a substitute
for a market-level regime table and retained as a supplementary column. The daily
table is built from `^VIX` and `^GSPC` and **populated**: 395 sessions,
2025-01-02 → 2026-07-31, five classes clearing n≥15, so `per_regime` becomes
answerable rather than unmeasurable. Mark-quality columns now travel with every
intraday grade, and 0DTE's re-enable bar became quantitative (≥80% RTH coverage,
max gap ≤5min) — intraday marks shipped and 0DTE stays suspended anyway, which
is the point of making the bar a number.

**P3/P4.** CI existed nowhere in this repository; the freeze was enforced by
tests nothing ran. The path guard deliberately watches provider files as well as
`scoring/`, because FINDING_01 changed the shipped model from
`unusual_whales/client.py` with zero diff under `scoring/`. Validated against
real history rather than asserted: the FINDING_01 range trips it and passes only
because declared; the Phase 2 range does not trip it at all. Freeze tag published
on `80eb42c`; `docs/FREEZE_POINT.md` records the SHA as the authority so the
check survives a clone without tags. Branch protection active on `main`.

**P5.** All 86 export columns documented, verified programmatically rather than
by eye. Adds the two-era note, without which `NA_no_data` in §1C reads as a
capability gap rather than an era gap.

### Decisions taken

1. **Fix FINDING_01 now, not later** (reviewer's call, and correct).
2. **DTE 0 excluded from the term-structure front leg** — my judgment, flagged in
   the PR as the most reviewable line in the diff.
3. **Regime join is strictly the PRIOR session.** A signal fired at 10:15 cannot
   know that day's close; joining to it would condition the pre-registered
   per-regime cuts on the future.
4. **Regime rows are never rewritten on re-run.** A row describes a closed
   session; a vendor revision is a human decision, not a silent re-cut of
   analyses that already grouped by it.
5. **`UW_INTRADAY_ENABLED` kept separate from `UW_HISTORIC_ENABLED`** — different
   endpoints, independently entitled.

### DEVIATIONS

**Not None.** Three.

1. **Ruling 1 step 2 could not be satisfied as written.** The ruling predicted the
   golden file would break on the term_slope fix. It does not, and structurally
   cannot: it scores fixed `IVContext` fixtures, so a provider change cannot move
   its numbers. That is inherent to a golden file rather than a flaw in this one —
   it pins what the scorer computes given inputs, while the provider-contract test
   pins which inputs production supplies. Equivalent proof came from the contract
   test (8 of 9 failing pre-fix, 9 of 9 after) and from the freeze guard
   independently catching the version bump (1 failed, 811 passed). Reported in the
   PR body and commit message rather than worked around.
2. **I told the user `freeze-guard` would not run on PR #50. It ran, and passed.**
   For same-repo PRs GitHub uses the workflow from the PR head, not the base. The
   caveat was wrong and was corrected in the next message.
3. **The "53-minute gap" figure I reported all session was imprecise.** The
   measured value is 52 UNOBSERVED minutes — consecutive bars are a 0-minute gap,
   not a 1-minute one. Same hole, tighter definition. The gate message and tests
   now use the number the code computes.

### Corrections to earlier reporting, carried forward from Entry 0

Item 1.3 was reported complete and was 3 of 4 (now closed by Ruling 1). Items 1.4
and 1.11 diverged from specification and were ruled on — 1.4 accepted for 1–5DTE
with mandatory quality columns and rejected for 0DTE; 1.11 rejected and rebuilt.

### State at entry close

- **Production has NOT been redeployed.** Still `7afa098`, now four merges
  behind. No deployed signal has ever carried capture gates, market context, or
  intraday grading.
- **Capture window has NOT started.** Zero signals captured. The clock starts on
  verified capture (P11), not on deploy.
- `main` at `34d0bad`; model `sd-scoring-2026.08-v3.1`; freeze point `80eb42c`;
  `git diff` over scoring paths since the freeze is empty.
- 853 tests passing, ruff clean, CI green on both jobs.
- `daily_regimes` populated in production (395 rows); nothing reads it until
  deploy.
- **Outstanding, unchanged and overdue:** rotate the UW API key and both Turso
  tokens. All three were exposed in a session transcript. The UW key is now more
  load-bearing than when exposed — it carries the intraday entitlement that
  replaced a $199/month vendor.
- Remaining pre-flight is entirely the human's: **P10** redeploy from `main`
  (set `UW_INTRADAY_ENABLED=true`), **P11** first-session verification, **P12**
  declare window start. **P9** (direction instrumentation 3.1–3.3) is
  non-gating and may land early-window.

---
