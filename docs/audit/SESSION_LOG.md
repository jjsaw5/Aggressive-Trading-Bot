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
## Entry 2 — 2026-08-01 — Review Packet #2: pre-flight closure + front-end display honesty

**Contemporaneous.**

### What changed

| Change | Why |
|---|---|
| `alembic/versions/0006_daily_regimes.py` — import sort | ruff I001. Found only because I linted `.` rather than CI's `app/ scripts/ tests/`. No behaviour change; migrations are outside the guarded scoring paths. |
| `review_packet_2/` — 37 files | The requested review packet. |

No source behaviour changed this session. The freeze controls were run and pass:
20 golden, 9 provider-contract, all freeze-import. Full suite **853 passed**.

### PRs

- **#52 opened and closed unmerged** (`claude/ci-pathguard-demo`, `9af9d12`). A
  deliberate throwaway: a comment-only edit to
  `app/shortduration/scoring/components.py` with no version bump and no
  amendment, to demonstrate the freeze path guard failing a PR. CI run
  `30715994614` concluded `failure`; job `tests` passed and only `freeze-guard`
  failed, printing the guarded path and `scoring_model_version bumped : 0`. The
  verbatim log is `review_packet_2/A_P3_ci_pathguard.txt` — the P3 evidence.

### Decisions taken

1. **The guard demonstration was run on a comment-only change, on purpose.** A
   comment alters no behaviour, so the guard failing it proves the guard gates on
   PATH and demands a human declaration rather than trying to judge semantics.
   That is the FINDING_01 lesson encoded: a behaviour change can arrive through a
   file no static check can reason about.
2. **P9 belongs before P10, not after.** Both are the user's actions and both are
   open. Stated in the packet: once production redeploys the window opens, and
   from that moment an undeclared scoring change contaminates a live corpus
   rather than an empty one — which is exactly when an advisory-only guard stops
   being sufficient.
3. **The ruff error was fixed rather than argued away, and reported rather than
   quietly corrected.** The interesting part is not the import order; it is that
   "CI is green" and "the repository is clean" were not the same statement, and
   `alembic/` sat in the gap.
4. **Front-end review reported two findings as stance violations and the rest as
   recommendations.** The two — `PICK #N` badges and the standalone
   `confidence %` — are the only elements where the UI asserts something the
   product stance denies. Everything else (timezones, colour thresholds,
   double-gate prose) is a real improvement but not a contradiction, and is
   labelled as such rather than inflated to match.
5. **No front-end change was made.** The request was a review. Changing the UI
   during it would have meant reviewing my own edit.

### Findings worth carrying forward

- **`confidence` is not an independent reading.** `engine.py:108` computes
  `overall = normalized * (0.6 + 0.4 * data_quality)`. The board renders
  `tradability`, `confidence` and `data quality` as three peer numbers; they are
  two. Reproduced exactly on 58 of 58 live rows, residual 0. The arithmetic is
  right; the *implication* of corroboration is false.
- **`PICK #1` badges** carry a recommendation in three registers (word, ordinal,
  row highlight) about sixty lines from the banner stating the product does not
  recommend.
- **11 timestamps render browser-local**, with `timeZone` pinned exactly once in
  the file. On a same-session-expiry product this is a correctness hazard.
- **The honest parts of the UI are the parts the server computed.** The
  UNCALIBRATED stamp, the abstain reason and the conviction note are all built in
  `engine.py` and relayed. The three problems are all things the browser decided
  for itself.

### DEVIATIONS

**Not None.** Three:

1. **The demo branch `claude/ci-pathguard-demo` could not be deleted.** PR #52 is
   closed, but `git push origin --delete` fails with `the remote end hung up
   unexpectedly` — the same credential scoping that blocked the P4 tag push. The
   branch still exists at `9af9d12`. It is unmerged, closed, and harmless, but it
   is residue and it is not cleaned up. Deletion needs the GitHub UI.
2. **Section B screenshots are mock-derived.** The local instance ran with all
   providers mocked against a temporary SQLite file. The layout, labelling,
   colour and claim text are exactly what the code renders; no price on those
   screens is real. Production could not be screenshotted regardless — it runs
   `7afa098`, a different front end.
3. **P9, P10 and P11 are open and were not attempted.** P9 needs repository
   settings; P10 needs the Docker host. Neither is reachable from this session.
   P11 is verified blocked at zero rows rather than assumed blocked.

### State at entry close

- Production still `7afa098`. Capture window still **not started**, zero signals.
- Model `sd-scoring-2026.08-v3.1`; freeze point `80eb42c`.
- 853 tests passing; ruff clean across the whole repository, not just CI's scope.
- `daily_regimes`: 395 rows, 2025-01-02 .. 2026-07-31, 49 `unknown` (contiguous
  SMA warmup, labelled rather than back-filled).
- **Outstanding, and now overdue:** rotate the UW API key and both Turso tokens.
  All three were exposed in a session transcript and all three are still live.

---

## Entry 3 — 2026-08-02 — Reviewer Rulings #2: the three pixel-level contradictions removed

**Contemporaneous.**

### What changed

| Ruling | Change |
|---|---|
| R1 | `docs/FREEZE_POINT.md` — the trio documented: which control catches which class of change, and why the golden file structurally cannot catch a provider change. Ruling 1 step 2 amended in place. |
| R2 | `CLAUDE.md` §4 — secret handling promoted into the honesty rules, where the live escape belongs. |
| R3.1 | `PICK #N` removed: badge, ordinal and `pickrow` highlight together, plus the CSS. `engine_pick`/`pick_rank`/`pick_reason` stay in the payload and warehouse. |
| R3.2 | Standalone `confidence` removed from the candidate detail. Not relabelled, not re-derived. |
| R3.3 | One ET formatter (`etDateTime`/`etTime`/`etDate`) replaces 11 bare `toLocale*` calls; `tests/test_dashboard_timezone.py` fails the build on any future bare call. |
| R3.4 | Colour bands served from `app/config.py` via `/config/runtime`, versioned `sd-display-2026.08-v1`, duplicated literals removed. |
| R3.5 | Execution prose rewritten to describe the build. |
| R3.6 | CI lints `.` instead of three directories. |
| R4.4 | `secret-scan` job (gitleaks) + `.gitleaks.toml`. |

860 tests pass. `ruff check .` clean. Freeze controls unchanged and passing.

### Decisions taken

1. **Three quantities are named `confidence`; only one was removed.** The regime
   engine's own read and `CatalystEvent.confidence` ("reliability of the
   CLASSIFICATION, not a trade signal") are independent measurements that happen
   to share a word. Checked the domain models before editing rather than
   grepping for the string and deleting every hit. The scorecard's
   `news_confidence`/`flow_confidence` component labels also stay — those are
   component names, not the derived scalar.
2. **The neutral-colour fallback is permanent, not interim.** The ruling allowed
   neutral colours until served thresholds existed. I built the served
   thresholds, and kept neutral as the behaviour when they fail to load: a
   colour is a claim, and a claim whose threshold we could not load must not be
   made. Boot now awaits config before the first paint, so a number cannot
   render one colour and be corrected under the reader.
3. **The gitleaks UW rule is anchored on the variable name, not on UUID shape.**
   Matching every UUID in the repository would produce noise, and a scanner that
   cries wolf trains people to add allowlist entries — which is how a scanner
   stops being read.
4. **Display bands live in `app/config.py` beside the model version.** The file
   is outside the freeze guard's guarded paths and nothing there reaches the
   scorer; the three freeze controls pass unchanged. Versioning them makes a
   change to "what counts as good odds" a dated, visible event.
5. **`docs/FREEZE_POINT.md` documents why the golden file is blind to providers
   rather than treating that blindness as a gap.** Hand-built fixtures cannot
   move when a provider changes — that is what makes the file a clean
   measurement of the arithmetic alone. The provider-contract test covers the
   other direction. Written down so the next reader does not repeat Ruling 1's
   mistaken prediction.

### DEVIATIONS

**Not None.** Two:

1. **My first post-fix verification reported a false failure.** I regexed
   `page.content()` for `PICK #` and got `true`, which would have meant the
   removal failed. It was matching my own explanatory comment in the served
   `<script>` block. Re-run against `innerText` of the rendered view — 0 badge
   elements, no `PICK #` text, across 50 rows of which 3 carried
   `engine_pick=true`. Recording it because the first result was the kind that
   gets reported as a defect.
2. **`claude/ci-pathguard-demo` still exists on the remote.** PR #52 is closed;
   `git push --delete` fails with the ref-scoping that also blocked the P4 tag
   push. Needs the GitHub UI.

### State at entry close

- Production still `7afa098`. Window still **not started**, zero signals.
- Model `sd-scoring-2026.08-v3.1`; freeze point `80eb42c`; display bands
  `sd-display-2026.08-v1`.
- 860 tests passing; ruff clean repository-wide.
- **R4 sequence: steps 1 and 2 (credential rotation, then branch protection) are
  Justin's and are the gate on everything after.** Step 3 (R3.1–3.3) is done,
  step 4 (CI secret scanning) is done. Steps 5–7 wait on 1 and 2.
- **Outstanding, overdue, and now the top of the reviewer's own ordering:**
  rotate the UW API key and both Turso tokens.

---
