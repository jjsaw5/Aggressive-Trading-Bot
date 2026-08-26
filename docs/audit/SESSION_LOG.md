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

## Entry 4 — 2026-08-03 — Entry-gate timing recomputed on read

**Contemporaneous.**

### The report

At 09:54 ET every row on the board read `time_of_day_blocked`. The question was
whether this was the market-settle window about to clear on its own.

It was not. The settle window is **5 minutes**
(`short_duration_no_entry_first_minutes = 5`), so it had cleared at 09:35.

### What was actually wrong

`evaluate_entry_gates` runs once, at scan time, and its verdict is frozen onto
the candidate (`detection.py:350/353`). Nothing re-evaluated it on read. A row
scanned pre-market or inside the opening window therefore carried
`time_of_day_blocked` for the rest of the session.

Production data, decoded from `short_duration_candidates.payload`:

| scan (UTC) | ET | rows | entry_allowed | time_of_day_blocked |
|---|---|---|---|---|
| 12:15:42 | 08:15 | 7 | 0 | 7 (pre-market) |
| 13:31:21 | 09:31 | 7 | 0 | 7 (opening window) |
| 13:46:33 | 09:46 | 7 | **7** | 0 |
| 13:52:54 | 09:52 | 7 | **7** | 0 |

The gate itself was behaving correctly at every instant. The board was showing a
09:31 verdict at 09:54.

### The fix

`app/shortduration/risk.py`
  - `timing_gate_now(dte, now)` — the clock rule alone; no market data, no I/O
  - `refresh_timing_gate(...)` — recompute timing, preserve everything else
  - `apply_live_timing(candidates)` — applied on the read path
  - `GATE_REJECTS` — the reject reasons `evaluate_entry_gates` can emit, so
    contract-level reasons are not mistaken for gate blocks

Wired into all four read endpoints (`0dte`, `1-5dte`, `medium`, detail).

`app/domain/shortduration.py` — `entry_gates_evaluated_at`, `entry_timing_is_live`.
`app/web/dashboard.html` — `gateAsOf()`; the ENTRY cell now reads
`ALLOWED (non-timing gates as of 4:01 PM ET, 1d ago)`.

`tests/test_entry_gate_timing_refresh.py` — 19 tests.

### Decisions taken

1. **Recompute, do not merely un-block.** The stale-permissive direction is the
   dangerous one: a 0DTE row scanned at 14:00 would have kept ALLOWED past the
   15:00 cutoff. Clearing stale blocks alone would have fixed the visible
   symptom and left the hazard. Two tests pin the re-blocking direction.
2. **Only timing is recomputed.** Liquidity, sizing, portfolio limits and regime
   describe the setup and the account at scan time; this function has neither
   the chain nor the account state to re-derive them, and guessing would be
   worse than freezing. They stay frozen and are labelled as frozen.
3. **`entry_allowed` keeps its scan-time semantics** — the GATE verdict alone.
   Contract rejects (`illiquid_option`) remain in `reject_reasons` and do not
   block entry, matching production rows that carry both `illiquid_option` and
   `entry_allowed=true`.
4. **A contract test ties `GATE_REJECTS` to the source of
   `evaluate_entry_gates`.** If that function grows a new reject reason, the
   test fails rather than the new reason being silently downgraded to advisory.
   Same technique as the provider-scoring contract test.
5. **The as-of label is always shown, not only when stale.** The reader should
   not have to know a threshold to know what they are looking at; the age suffix
   appears past 15 minutes.

### Freeze status

No scoring change. `app/shortduration/risk.py` is not a guarded path and the
scorer neither reads nor is read by the entry gate. Golden, provider-contract and
freeze-import tests unchanged and passing.

### DEVIATIONS

**Not None.** Two:

1. **Credential rotation remains incomplete, by the owner's explicit decision.**
   The pre-rotation Turso token still authenticates against production (verified:
   40,154 snapshots readable). New tokens were minted; the old ones were never
   invalidated. Raised twice, the owner judged it non-blocking for app function —
   which is correct — and deferred it. Recorded here because a governance
   deviation agreed in conversation is not recorded anywhere a future session
   can see it. The invalidating command is
   `turso db tokens invalidate <database>`.
2. **The stale-gate defect reached production and was found by a user, not by a
   test.** No test asserted that a gate verdict is current when displayed. That
   gap is now closed for timing, but the same freeze-and-display pattern applies
   to `freshness` (which has its own on-demand endpoint) and to
   `market_context`; neither was audited this session.

### State at entry close

- `main` at `eb1f6de` (PR #53 merged). **Production redeploy still not
  confirmed** — see the note below on why it may never have taken.
- **`docker compose up -d` without `--build` does not pick up code changes.**
  The Dockerfile does `COPY . .` and the compose file mounts no volumes, so the
  running container serves the image as built. Compose only builds when the named
  image is absent. This is the likely reason production stayed on `7afa098`
  across several "deploys". The correct command is
  `docker compose up -d --build`.
- 879 tests passing; ruff clean repository-wide.
- Model `sd-scoring-2026.08-v3.1`; freeze point `80eb42c`.

---

## Entry 5 — 2026-08-03 — Amendment 2 approved: selection prices probability

**Contemporaneous.** Model `sd-scoring-2026.08-v3.1` → **`sd-scoring-2026.08-v4.0`**.

### Origin

A user question — "why do we not see the app surface plays like this? Where it's
reasonable to enter 87 dollars and short duration on something like SPY?" — with a
screenshot of a SPY 759/761 3-DTE spread at $0.87.

### What was wrong

The selector's fit function was `0.7 x reward-to-risk + 0.3 x width`. Probability
appeared nowhere, and both terms rise as a structure moves out of the money, so
its maximum was by construction the cheapest far-OTM spread the chain allowed.

Its own record of the SPY structure it chose: `POP 0.1044, R:R 26.62, "SPY must
rise 4.4% to $790.90 by expiry (28d) just to break even."` It computed that
number, stored it, displayed it, and did not use it to choose.

Systemic: 64 structures priced 2026-08-03, median POP **0.287**, median R:R
**7.79:1**, **45% below POP 0.25**.

### Approved and shipped (C1–C4 in full)

| | |
|---|---|
| C1 | `fit = 0.45*POP + 0.35*min(1, rr/2) + 0.20*width_term` |
| C2 | hard POP floor 0.25 — REJECTS; unmodellable odds cannot clear it |
| C3 | short-leg delta floor 0.15 |
| C4 | horizon by thesis (`short_horizon_viable`), not by strategy label |

Plus the §5 guard fix, landed **first and separately** so this change had to clear
it.

### The mechanism found mid-implementation, which was the real culprit

C1–C3 were written against the fit function. Building the regression test showed
the fit function was not what put a 0.10-delta leg on the board.

`_leg_ok` in `select_vertical_spread` fell through to the moneyness fallback
**whenever the delta band check failed** — not only when delta was missing or
degenerate, which is what `_moneyness_fit`'s own docstring promises and what
`select_long_contract` actually implements (`money = ... if not by_delta`). So a
leg with a perfectly good provider delta *outside* the band was re-admitted on
strike proximity alone.

That is exactly how SPY 790 (delta **0.1035**, 4.4% from spot, inside the 6% swing
moneyness band) became a long leg under a 0.30 delta floor. Fixed to match the
docstring and the sibling function.

### Decisions taken

1. **The POP floor rejects rather than down-ranks, and `None` cannot clear it.**
   A probability we could not compute is not evidence of a good one. Two tests
   pin this, including one proving the rejection is the FLOOR and not a crash in
   pricing.
2. **A new reject reason, `POP_BELOW_FLOOR`, with a truthful message.** Without it
   a floor rejection would have rendered as "No defined-risk structure fits the
   $98.15 per-trade cap" — false, and precisely the class of lie this product
   exists to avoid. `_blocked_only_by_pop_floor` re-runs selection with the floor
   lifted to tell "we found nothing" from "we found only long shots"; the cost is
   paid only on the rejection path.
3. **A MAJOR version, not a point release.** v3→v3.1 changed whether a coefficient
   could fire; this changes which instrument a signal is expressed in. A v3.1 row
   and a v4.0 row on the same symbol and minute are different trades and must
   never pool.
4. **The test fixture now records `implied_volatility=VOL`.** The chain was
   already Black-Scholes-priced at VOL, so this makes the fixture self-consistent
   rather than accommodating the new floor — without it POP is unmodellable and
   every structure is correctly rejected.
5. **A test asserting the amendment barely changes a well-behaved chain.** The fix
   is targeted at the fallback path; its failure would mean over-reach.

### Golden-file delta

Regenerated: **only the `model_version` string changed — 9 leaves, no computed
value moved.** Same structural limitation as Amendment 1 — the golden file scores
fixed `IVContext` fixtures and passes no trade plan, so a *selection* change
cannot move its numbers. Reported, not worked around. The controls that do cover
this class are `tests/test_contract_selection_amendment2.py` (17 tests) and the
now-widened path guard.

### DEVIATIONS

**Not None.** Three:

1. **My first two regression tests asserted a premise the fixture did not
   produce**, and failed. The synthetic chain has clean in-band deltas, so the
   legacy rule picked a sane structure there and the "amendment improves odds"
   comparison was vacuous. Rewritten against a SPY-shaped fixture that reproduces
   the real inputs (spot 756.37, IV 0.13, 28 DTE), with a guard test asserting the
   fixture really does place a ~0.10-delta strike inside the moneyness band.
   Recorded because the first version would have passed for the wrong reason had
   I chosen a looser assertion.
2. **The proposal predicted the golden file might move. It did not, and could
   not.** The prediction was repeated from Amendment 1's ruling without
   re-checking that the same structural limitation applied. It does.
3. **Credential rotation still incomplete** (owner's deferral, Entry 4). The
   pre-rotation Turso token still authenticates against production.

### State at entry close

- Model `sd-scoring-2026.08-v4.0`. Freeze point `80eb42c` still names v3.1 and is
  now historical rather than current — **`docs/FREEZE_POINT.md` needs a new tag
  and SHA once this merges.** Not done in this commit; flagged.
- 895 passed, 1 skipped. `ruff check .` clean.
- Production still not confirmed redeployed; `docker compose up -d` without
  `--build` does not pick up code changes.
- The capture window restarts from zero under v4.0. Nothing was lost — no signal
  was ever captured under v3.1.

---

## Entry 6 — 2026-08-04 — Freeze point advanced to v4.0; the guarded set given one home

**Contemporaneous.** Follow-up to Entry 5, flagged there and done here.

### What was wrong

After Amendment 2 merged (`935160d`), `docs/FREEZE_POINT.md` still named
`sd-scoring-2026.08-v3.1` at `80eb42c`. Two consequences, both worse than a stale
doc:

1. **CI hardcoded the tag name** (`REF="freeze/sd-scoring-2026.08-v3.1"`), so the
   informational step compared current work against a *superseded* baseline.
2. **That step diffed two directories** — `scoring/` and `strategies/` — while the
   blocking path guard had been widened to seven paths. So on PR #55, the run
   that correctly blocked on `contract_selection.py` **also printed "Empty —
   scoring paths unchanged since the freeze"**, about paths it was not looking at.

Reassurance from a check that is not looking is worse than no check.

### What changed

- **Freeze point advanced**: model `sd-scoring-2026.08-v4.0`, tag
  `freeze/sd-scoring-2026.08-v4.0`, commit `935160d`. Prior points kept in a
  **Superseded** table so an old `scoring_model_version` stamp stays traceable.
- **One home for the guarded set**: `GUARDED_RE` and `GUARDED_PATHS` at the top of
  the `freeze-guard` job. Both steps read them; neither redefines them.
- **CI reads the tag name and SHA from the doc**, so the baseline cannot go stale
  independently of the record.
- **`tests/test_freeze_guard_config.py`** — 12 tests asserting the regex, the path
  list and the documented hand-check describe the same set; that every guarded
  path still exists; that the doc header names the configured model version; and
  that the first SHA/tag in the doc are the header's, not a superseded row's.

### Decisions taken

1. **Document ordering is now a correctness property, and is tested.** CI takes
   the *first* 40-hex SHA and *first* `freeze/` string from the file. Adding a
   superseded-points table introduced a way to break that silently, so two tests
   assert the first of each falls above the `## Superseded` heading.
2. **The negative control was run before trusting the new test.** I reintroduced
   the exact drift — removed `contract_selection.py` from `GUARDED_PATHS` only —
   and confirmed 2 tests fail, then restored. A sync test that has never been
   shown to fail is an assumption.
3. **Superseded freeze points are kept, not deleted.** A row stamped v3.1 must
   remain traceable to the code that produced it; the table says explicitly that
   they are history, not baselines.
4. **The `## Scope` section now says the path list is a lagging indicator.** It
   has grown twice, each time *after* a model change slipped past it. Stating that
   is more useful than implying the list is complete.

### DEVIATIONS

**Not None.** Two:

1. **The tag itself is not pushed.** `freeze/sd-scoring-2026.08-v4.0` needs
   publishing via the GitHub Releases UI — the session credential is scoped to
   `refs/heads/*`. This is the same limitation the document was written to
   survive: the SHA is the authority and CI falls back to it, so the check works
   in the meantime and says so in its output.
2. **The stale baseline was live for one merge.** PR #55 merged while the
   informational step still pointed at v3.1. Nothing was mis-gated — the blocking
   path guard was correct throughout — but the run printed a reassuring line that
   was not evidence of anything.

### State at entry close

- Freeze point `sd-scoring-2026.08-v4.0` at `935160d`; tag pending publication.
- 907 passed, 1 skipped. `ruff check .` clean.
- Production still not confirmed redeployed. Capture window restarts from zero
  under v4.0.
- Credential rotation still incomplete (owner deferral, Entry 4).

---

## Entry 7 — 2026-08-04 — Amendment 3: 0DTE captured as observation-only

**Contemporaneous.** Model `sd-scoring-2026.08-v4.0` → **`sd-scoring-2026.08-v4.1`**.

### Request

"I want to enable 0DTE options, while we may not be taking them I think having
the data populate for paper trading to zero in on our logic is the right thing
to do."

### What was in the way, and what actually needed building

Suspension dropped 0DTE setups **before scoring** (`detection.py:477` `continue`),
so no record existed at all. Ruling 2 imposed that because 0DTE **grades** are
uninterpretable — 31% session coverage, 52-minute maximum gap.

Investigating before changing anything found the real gap: **`calibration.py`
filtered on `scoring_model_version` alone.** P7 attached `mark_coverage_pct`,
`max_gap_minutes` and `grade_confidence` to every outcome and **nothing consumed
them**. So the suspension was a blunt instrument standing in for a quarantine
that did not exist — and simply flipping the flag would have done exactly the
harm Ruling 2 was preventing.

### What changed

| | |
|---|---|
| `calibration.gradeable_outcomes()` | drops outcomes with `grade_confidence` low/unknown |
| `calibration._drop_observation_only()` | drops decisions whose `dte_bucket` is observation-only |
| `DecisionSnapshot.dte_bucket` | new field; the bucket is RECORDED, not inferred |
| `capture_observation_only_buckets` | new config; `0dte` moves here from suspended |
| `capture_gates.is_observation_only()` / `observation_only_note()` | state + a reason in numbers |
| `detection` arm block | generalised from a 0DTE special case to the config |
| `_SD_VERSION_RE` | fixed: dotted versions were unparseable since v3.1 |

Both exclusions are counted and surfaced as scorecard warnings.

### Decisions taken

1. **The bucket is recorded, not inferred.** `dte_at_entry` cannot identify a
   bucket: the 0DTE selector admits dte 0 OR 1 and the 1-5DTE selector starts at
   1, so filtering on the integer would silently drop legitimate 1-5DTE rows. A
   test pins that exact case.
2. **Two independent quarantines, deliberately.** A 0DTE decision graded from
   DAILY marks carries the pre-P7 empty confidence string and passes the
   confidence filter while being precisely the uninterpretable case. The bucket
   filter is what catches it; a test asserts each catches what the other misses.
3. **Empty `grade_confidence` is treated as gradeable.** It is the pre-P7 default
   — unknown-but-not-known-bad. Excluding it would silently discard the entire
   corpus predating the measurement.
4. **The quantitative bar is untouched.** It now governs PROMOTION out of
   observation-only rather than whether the bucket exists. This is why the change
   is not read as overturning Ruling 2.
5. **A declared "Pending freeze point" section** was added to
   `docs/FREEZE_POINT.md`. `test_freeze_guard_config.py` requires the header to
   name the configured version, but a freeze point's SHA is its merge commit and
   cannot exist in the PR that creates it. The pending block makes that gap a
   declared state rather than a test to weaken, and deliberately contains no
   `freeze/` string and no 40-hex SHA so the machine-read parse still resolves
   v4.0.

### DEVIATIONS

**Not None.** Two:

1. **I repeated a known incident: an ad-hoc `run_detection` warehoused 12 rows to
   the live corpus.** Verifying that 0DTE now produces candidates, I ran detection
   without blanking Turso. Unlike the earlier 84-row case these were REAL
   (`chain_source=unusual_whales`), not mock — but they were produced by an
   unmerged working tree and stamped `v4.1`, which would have made the first rows
   of the new model's corpus a test artifact.
   **Purged**: 12 snapshots, 12 candidates, 36 transitions; verified 0 remaining.
   `scripts/purge_mock_signals.py` gained a `--model-versions` discriminator,
   because its existing `chain_source=mock` key did not match this class and the
   next occurrence should not need a one-off script.
   **The standing lesson is unlearned so far**: `run_detection` persists
   unconditionally, and nothing prevents a developer invoking it against the
   production warehouse. That is a real control gap, not merely my mistake — it
   has now caused two incidents. Not fixed in this change; flagged.
2. **Five existing tests asserted the old behaviour and were rewritten**, not
   deleted: 0DTE suspension by default, gate precedence, the 0DTE rejection
   message, and two version pins. Where a test existed to prove a MECHANISM (the
   rejection message states the bar in numbers), suspension is now configured
   explicitly inside the test so the mechanism stays covered.

### State at entry close

- Model `sd-scoring-2026.08-v4.1`; freeze point v4.0 at `935160d`, **v4.1 pending
  its merge commit**.
- 931 passed. `ruff check .` clean.
- Production still not confirmed redeployed; capture window not started.
- Credential rotation still incomplete (owner deferral, Entry 4).

---

## Entry 8 — 2026-08-07 — Trade evaluator: grade a trade the human proposes

### What changed and why

A new surface answering a different question from the scanner. The scanner ranks
what fits **this account** ($100/trade, $300 heat, 4 positions). The evaluator
takes a ticker, a structure and a duration — optionally strikes — and grades the
**trade**, with the account deliberately out of scope.

This is a closer fit to `docs/PRODUCT_STANCE.md` than the scanner is. The stance
says *"the thesis is the human's; the tool makes it cheaper to evaluate."* That
is this feature's job description; the scanner generates theses, this evaluates
the owner's.

| Added | Purpose |
|---|---|
| `app/domain/evaluation.py` | request/result models; sentinels; the grade's disclaimer as a field, not a UI string |
| `app/engine/trade_evaluator.py` | the rubric — six dimensions, horizon resolution, structure pricing, selector contrast |
| `app/research/evaluate.py` | provider fan-out (chain, IV, earnings, quote), per-section error isolation |
| `POST /research/evaluate` | the endpoint; the ONLY writer |
| `trade_evaluations` table + migration `0007` | quarantined persistence |
| `docs/TRADE_EVALUATOR.md` | the rubric written down, including what it does NOT claim |
| Dashboard → Evaluate → Trade Evaluator | the screen |
| `tests/test_trade_evaluator.py` (44), `tests/test_trade_evaluator_isolation.py` (10) | rubric + the control |

**Reuse over rebuild.** The analysis primitives already existed as standalone
functions (`quant/probability.py`, `quant/analytics.py`, `engine/iv_context.py`,
`engine/liquidity.py`, `engine/catalysts.py`) and the concurrent per-symbol
fan-out pattern was already proven in `app/research/symbol.py`. The new code is
the rubric and the isolation, not the plumbing.

### Decisions taken, with reasoning

1. **The grade is of CONSTRUCTION, not outcome.** The conviction gate is RED, so
   nothing here may predict profit. What needs no calibration to be true: cost
   arithmetic, odds at the market's own implied vol, execution cost, IV context,
   scheduled-event conflicts. `grade_claim` ships as a model field so the caveat
   travels with the number to every consumer, not just to the one screen that
   currently renders it.
2. **Unassessed dimensions score `None`, not 0.0.** A missing feed that silently
   contributed zero would be indistinguishable from a measured failure — CLAUDE.md
   §4. The composite renormalizes over assessed dimensions and the report always
   states the count, because a B over four of six is a different claim from a B
   over six.
3. **Any single `fail` caps the grade at D.** The far-OTM lottery ticket is why:
   verified live, an 800/805 call spread scored **strong** on cost drag (13% of
   width, 6.5:1 R:R) and **failed** on probability at 12%. That is exactly the
   trap the pre-Amendment-2 fit function fell into, and a plain average returns a
   B for it.
4. **`trade_eval_version`, NOT `scoring_model_version`.** The evaluator calls the
   frozen scorer's neighbours read-only but produces a different artifact.
   Borrowing the frozen version would make an evaluator change look like a change
   to the shipped model. Guarded-path diff against `935160d` is empty and all
   three freeze controls pass unchanged; **the capture window is unaffected**.
5. **Persistence is quarantined in its own table.** A user can evaluate the same
   bad idea forty times; counting those as decisions would move the base rate the
   conviction gate is measured against. `calibration.py` does not read the table.
6. **The account limits are removed in two places, one of them non-obvious.**
   `strategy_selector.py:50` is the visible one. The subtle one is
   `OptionLiquidityConfig.max_mid_price = 25.0`, commented *"keeps 1-lot
   affordable for small acct"* — a budget constraint wearing a liquidity costume.
   Genuine liquidity floors stay.
7. **The horizon resolves to a LISTED expiry and says which.** "3d" on a Thursday
   and "3d" on a Monday are different contracts. An unreadable horizon returns a
   gap with a reason rather than a default, because a default would silently
   evaluate a different trade from the one asked about.

### DEVIATIONS

**Not None.** One, and it is a repeat:

1. **A third ad-hoc run reached production.** Verifying the endpoint end to end,
   I started a local server with `DATABASE_URL` pointed at a scratch sqlite file.
   `.env` is loaded automatically and `TURSO_DATABASE_URL` takes precedence over
   `DATABASE_URL` in `app/db/session.py`, so the server connected to the
   production warehouse. `create_all` auto-created `trade_evaluations` there and
   **6 mock-data evaluations were written**.
   **Purged**: all 6 by id; verified 0 remaining. `decision_snapshots` (61,331),
   `short_duration_candidates` (1,629) and `candidate_state_transitions` (5,764)
   were unchanged — the isolation design contained the blast radius to the one
   quarantined table, which is the strongest evidence available that the design
   is right. Correct local invocation is
   `TURSO_DATABASE_URL= TURSO_AUTH_TOKEN= DATABASE_URL=sqlite:///...`, and the
   verification was re-run that way with the isolation confirmed behaviourally
   (2 evaluation rows, 0 in every signal table).
   **This is the same class as Entry 7's deviation and the 84-row case before
   it — three incidents from the same root cause.** `.env` silently outranks the
   override a developer reaches for. That is an environment-safety gap, not three
   independent mistakes, and it remains **unfixed**. The cheapest real fix is a
   startup guard that refuses a non-production process against a Turso URL unless
   an explicit opt-in is set.

**Also fixed, pre-existing and unrelated to this feature:**
`tests/test_trade_management.py::test_quick_add_accepts_an_inline_invalidation`
was failing on clean `main` (verified by stashing this work). It asserted a
weeks-out expiry classifies as `swing` but wrote the expiry as the literal
`8/21`, which was comfortably swing when authored and became a 14-DTE `theta`
position as the calendar advanced past `THETA_MAX_DTE = 15`. A date-relative
assertion pinned to an absolute date fails on a schedule rather than on a defect.
The expiry is now derived from `date.today()`, so the test asserts the classifier
instead of the calendar. The product code was correct; only the test moved.

Also noted, not a deviation: two verification steps initially reported false
passes and were redone — a `node --check` on a process substitution that printed
"OK" from the shell rather than from node, and a budget-blindness test whose
fixture contained no contract the affordability cap would have excluded. Both
were caught by guards written into the checks themselves.

### State at entry close

- Model `sd-scoring-2026.08-v4.1` **unchanged**; guarded-path diff vs `935160d`
  empty; freeze controls green. Evaluator ships at `trade-eval-2026.08-v1`.
- `docs/FREEZE_POINT.md` still carries the declared "Pending freeze point" block:
  the v4.1 merge commit `f9f98f0` now exists but the tag
  `freeze/sd-scoring-2026.08-v4.1` is **not published**. Blocked on the owner —
  the pushing credential is scoped to `refs/heads/*`.
- Credential rotation still incomplete (owner deferral, Entry 4).

## Entry 9 — 2026-08-10 — HIMS research; three evaluator defects found by live use

### What changed and why

Owner asked whether there was a HIMS trade around tonight's earnings. Answering
it against live FMP + Unusual Whales data exercised yesterday's trade evaluator
on a real name for the first time and surfaced three defects in it. All three
are fixed here; **none touches a guarded path** and the guarded-path diff against
`935160d` is empty with all freeze controls green.

| Defect | Effect | Fix |
|---|---|---|
| IV rank never joined | IV dimension reported `NA_no_data` on **8 of 8** symbols probed, while a rank was derivable from 251 history points | `gather_inputs` now builds the context through `build_iv_context`, the same join both scan paths use |
| Chain fetch centred on 30 DTE | **SPY's shortest reachable expiry was 7 DTE** — the evaluator could not price the 0DTE trades it is most often asked about | horizon resolved first, its neighbourhood fetched via `get_option_chain_for_expirations`, unioned with the default chain (probe bounded to 8 dates) |
| Whole-day time to expiry | `prob_finish_above` returns None for `days<=0`, so **every** 0DTE structure reported no probability — the heaviest dimension blank on the target use case | `years_to_expiry_days` returns the fraction of the session remaining on expiration day; 0.0 after the close, whole days otherwise |

Verified live: SPY 0DTE 772/773 went from unpriceable → **grade C, 5/6, POP
0.4665**; HIMS went from 5/6 → **6/6** with `iv_rank=0.4468` from `iv_history`.

### Decisions taken, with reasoning

1. **The provider's 30-DTE centring was left alone.** `_chain_expirations` lives
   in `app/providers/unusual_whales/client.py`, a guarded path, and changing it
   would change what the SCANNER sees — a model change under the freeze. The
   evaluator instead composes existing public provider methods. Cost: extra
   requests per evaluation, bounded at 8.
2. **I initially mis-diagnosed the IV-rank gap as a scorer defect and said so.**
   `iv_rank` IS a scored field (`scoring/components.py:123` abstains without it;
   `data_quality.py:41` gates on it), and the raw provider returns null for every
   symbol — which looked exactly like FINDING_01. It is not: `detection.py:417`
   and `candidate_builder.py:117` both join an IV history through
   `build_iv_context`, so production scoring has always had rank. **The gap was
   entirely mine**, in the evaluator's own fetch path. Corrected to the owner
   before any change was made. The near-miss is worth recording: the symptom of a
   real freeze-ending finding and the symptom of a new consumer skipping a join
   are identical from the provider call alone.
3. **The 0DTE fractional-day fix was in scope, not scope creep.** Fix #2's stated
   purpose was "the evaluator can price a 0DTE trade". Delivering reachability
   while the probability dimension stayed blank would have satisfied the letter
   and not the purpose.

### Research findings recorded (no trade taken)

- Earnings **2026-08-10 pm**, confirmed by both FMP and Robinhood — the owner's
  premise had it later in the week.
- Term structure steeply backwardated: **8/14 129% / 8/21 107% / 9/18 91%**,
  `term_structure_slope −0.455`. Implied move 14.2%; our feed reproduced the
  owner's Robinhood straddle to the cent ($4.41).
- Six prior prints: mean |D+1| move **11.8%**, median 13.2% vs 14.2% implied.
  **4 of 6 continued the D+1 direction through D+5.** n=6 on one symbol —
  anecdote-grade, explicitly not evidence, and recorded as a hypothesis only.
- Flow gave no directional edge: $2.01M, 26 calls / 24 puts, **zero sweeps**.
- Every priceable structure graded **C or D**; the best carried a **40%
  round-trip spread tax**. Recommendation was to take no position into the print,
  which the owner accepted.
- `docs/VRP_STAGE2_RESULT.md` already answers the short-vol side: execution cost
  ≈$13.6/trade exceeds the harvestable premium. The earnings-specific
  pre-registration (`docs/earnings_vrp_preregistration.md`, registered
  2026-07-28) remains unrun — flagged that a discretionary trade tonight would
  be trading ahead of it.

### DEVIATIONS

**Not None.** One:

1. **I told the owner the IV-rank gap looked like a scorer-level defect before
   confirming it.** The claim was wrong — the scanner performs the join and the
   scorer has always received rank. I corrected it in the next message and before
   touching any code, but the sequencing was backwards: the diagnosis should have
   preceded the report. Recorded because "report gaps, don't approximate them"
   applies to my own findings too, and an overstated finding against a frozen
   scorer is expensive noise.

Also noted, not a deviation: all research ran with `TURSO_DATABASE_URL=` and
`TURSO_AUTH_TOKEN=` blanked, against a scratch sqlite file, following Entry 8's
incident. Nothing was written to production.

### State at entry close

- Model `sd-scoring-2026.08-v4.1` unchanged; guarded-path diff vs `935160d`
  empty; all freeze controls green. Evaluator `trade-eval-2026.08-v1`.
- `docs/FREEZE_POINT.md` still carries its declared pending block — the v4.1 tag
  is still unpublished (owner action, `refs/heads/*`-scoped credential).
- Credential rotation still incomplete (owner deferral, Entry 4).
- Environment-safety guard from Entry 8 still **unfixed**.

---

## Entry 10 — 2026-08-21 — Amendment 4: the $100 cap was suppressing single legs

### What the owner asked for

Two things, phrased as two:

> "I want to review the options it's presenting us. Most of them are limited to
> spread. I'd like to change the logic to present single leg options and expand
> the limit of each options to a max cap of 500 dollars per option with an
> overall operating budget of 25,000"

They turned out to be one thing. **No logic change was needed to present single
legs.** `select_short_duration_contracts` has always appended both expressions —
its docstring says so: *"EVERY viable defined-risk expression for the setup — the
near-ATM single leg AND the defined-risk debit vertical."* Single legs were being
eliminated by arithmetic, downstream, in `build_long_option_plan`, which returns
`None` when even one contract exceeds the per-trade cap. At $100 a near-ATM
single leg is unsizeable on any liquid name, so the board showed verticals only.

Measured on a synthetic 2-DTE fixture (spot 250, IV 35%, ATM call ≈ $2.61):

| per-trade cap | expressions returned |
|---|---|
| $100 | `bull_call_spread` ×1, risk $89 (252/255) |
| $500 | `long_call` ×1, risk $261 (250) **and** `bull_call_spread` ×2, risk $392 (250/256) |

The cap, not a structure preference, was the filter. Raising it restores single
legs as a side effect — and, note the second row, it also **moves the spread the
scanner picks** (252/255 ×1 → 250/256 ×2). That second effect is why this is a
model change and not a config tweak.

### Why this bumps the model version

`sd-scoring-2026.08-v4.1` → **`sd-scoring-2026.08-v5.0`**.

The frozen scorer's arithmetic is untouched. But `contracts.py:192,221` pass
`max_debit_usd=policy.max_trade_risk_usd` into selection, and
`scoring/components.py:184` reads `rr = plan.risk.reward_to_risk` off the plan
that selection produced. Change the cap and a different contract reaches the
scorer, so a different score ships. Per CLAUDE.md §2, **the freeze is about
behaviour, not about which files you edited** — this ends the v4.1 window and
opens v5.0. Amendment recorded under `CAPTURE_WINDOW_PREREGISTRATION.md` §8,
dated, with the fixture table above.

### The third instance of the same defect

This is now the **third** time a change outside the guarded path list has moved
the shipped model:

| | what moved | where it lived |
|---|---|---|
| FINDING_01 | `term_structure_slope` populated | a provider |
| Amendment 2 | contract selection | `contracts.py` |
| **Amendment 4** | risk limits | `app/config.py` |

Each time the golden file stayed byte-identical on every number, because it
scores hand-built `IVContext` fixtures and **passes no trade plan**. That is a
structural blind spot, not bad luck: nothing about selection can move a golden
number. Recorded as such in the amendment and in `FREEZE_POINT.md` under a new
section, *"What the path diff does NOT cover — read before trusting a green
guard."*

The missing control is now written: **`tests/test_risk_limits_freeze.py`** (5
tests) pins the six limit values, the two resolved caps, which cap binds
(absolute $500 over the 5% pct cap, which would be $1,250 at the new equity), and
that the declaration matches `FROZEN_MODEL_VERSION`. Changing a limit now fails a
test that names the version, the same way a scoring edit does.

**`tests/test_single_leg_expressions.py`** (7 tests) pins the finding itself:
`_strategies(100.0) == {BULL_CALL_SPREAD}` and
`_strategies(500.0) == {LONG_CALL, BULL_CALL_SPREAD}`, plus an AST/source check
(`test_no_structure_preference_was_changed_to_achieve_this`) asserting the single
leg came back from the budget and not from a thumb on the structure scale.

### Limits, before and after

| | v4.1 | v5.0 |
|---|---|---|
| account equity | $2,000 | **$25,000** |
| max risk / trade | $100 | **$500** |
| aggregate heat (15%) | $300 | **$3,750** |
| concurrent positions | 4 | 4 (unchanged) |
| contracts / trade | 20 | 20 (unchanged) |

`docs/RISK_POLICY.md` rewritten for the new numbers, which also removed the §9
contradiction CLAUDE.md has been carrying (the limits table said 5%/$100 while
the prose below argued against a $40 cap). CLAUDE.md §9 updated: struck through
as resolved, and a new bullet added recording that the CI `freeze-guard` job
gates on **path**, so a guarded-set diff can be empty while the model moves.

### Golden file

Regenerated. **Only the nine `model_version` strings changed.** Every composite
and every component is byte-identical — expected, and for the reason above, not
reassuring. The delta is documented in the amendment as evidence of the blind
spot rather than as evidence of safety.

### Test fallout, and what it taught

Three tests failed after the bump. All three were **hardcoded literals of values
that had just moved**, not real breaks:

- `test_sd_validation.py`: `assert base.max_trade_risk_usd <= 100`
- `test_contract_selection_amendment2.py`, `test_observation_only_buckets.py`:
  version strings written out longhand

Fixed by **deriving rather than restating** — the first from
`settings.max_defined_risk_per_trade_usd` (plus a relative "genuinely lifted"
assertion that survives the next change), the other two by importing
`FROZEN_MODEL_VERSION` from `test_scoring_freeze`. A test that restates a
constant asserts nothing about behaviour and fails on a schedule.

Full suite: **1011 passed**. `ruff check .` clean.

### Corpus segmentation

Signals captured under v4.1 and under v5.0 are **not poolable** — the selection
input differs. Noted in the amendment so the capture-window analysis segments on
`scoring_model_version` rather than pooling by date.

### DEVIATIONS

**None.**

Noted, not deviations:

- All verification ran with `TURSO_DATABASE_URL=` / `TURSO_AUTH_TOKEN=` blanked
  against a scratch sqlite file (Entry 8's incident). Nothing touched production.
- The environment-safety guard proposed in Entry 8 — refuse a non-production
  process against a Turso URL without explicit opt-in — is **still unfixed**, now
  flagged in three consecutive entries.

### State at entry close

- Model **`sd-scoring-2026.08-v5.0`**. Freeze point pending publication.
- **Two** freeze tags now outstanding, both owner actions (the session credential
  is `refs/heads/*`-scoped and cannot push tags): `sd-scoring-2026.08-v4.1` at
  `f9f98f0`, and `sd-scoring-2026.08-v5.0` at this commit. `FREEZE_POINT.md`
  records both SHAs so the check works without the tags.
- Execution still gated off; conviction gate still RED. Nothing here places a
  trade — this changes what the board is allowed to *propose*.
- Credential rotation still incomplete (owner deferral, Entry 4) — the UW key and
  both Turso tokens were verified live earlier in this session.

---

## Entry 11 — 2026-08-25 — rh_sync lost a quarter of the trade history; purge and rebuild

### How it surfaced

Not from a test. From a scheduled sync reporting a number that disagreed with
its own input. The 2026-08-24 market-close run wrote 15 `created_closed` lines
whose P&L summed to **+$297**, and the database afterwards held **-$155** for the
same episodes. Checking the two against each other is not part of the routine;
it happened because the report was being read closely enough to notice that the
same episode id appeared three times.

### The defect — two leaks, one root

Nothing distinguished one round trip on a contract from another round trip on
the **same contract in the same session**.

1. `Episode.trade_id` hashed `(symbol, legs, open DATE)`. Trading TSLA 355c twice
   in a day produced one id for both round trips, and `save_paper_trade` is
   last-write-wins, so the second silently replaced the first. Four of seven
   TSLA episodes on 08-24 stored the final round trip's P&L instead of the sum.
   In every case the stored value was exactly the last listed line.

2. `_matches_tracked` is deliberately fuzzy — same symbol, same contracts,
   opened within 5 days — so a manual entry reconciles with its broker-side
   view. Unbounded, **one tracked row claimed every re-entry on the contract**,
   and the extras reported as `already_tracked` with their P&L never entering
   the corpus.

The second was only found because the first fix's dry run came back with
everything `already_tracked` — a result that looks like success and is not. Had
(1) shipped alone it would have moved the loss from an overwrite to a silent
skip rather than removing it.

**A realized number that vanishes is the same class of failure as substituting
0.0 for a missing one: the row looks complete and is wrong.** That is the honesty
rule in §4, arriving through a code path nobody had pointed it at.

### The fix (`307bfb9`)

Re-keyed on the episode's **opening broker order id** — unique because an order
is a single event, stable across re-runs because it comes from the broker, so
idempotency (the entire purpose of the id) survives. Falls back to the opening
timestamp where no order id parsed, rather than quietly re-colliding on the date.
Tracked-row matching is now one-to-one: a claimed row leaves the candidate pool.

Four tests, **each confirmed to fail against the pre-fix code** — the collision
test reproduces on `rhe9177aa9a2`, a real episode id from the 08-24 production
run. Verifying that a regression test actually regresses was the step that would
have caught this class of bug earlier, and is worth making habitual.

### The rebuild — a destructive production operation

Re-keying changes every episode id, so a plain re-sync would have ADDED correct
rows beside the stale ones. The owner was told this, held the 08-25 market-open
sync unapplied rather than let it double-count, and then authorised the purge.

Backup first: 161 trades, 110 outcomes, 68 snapshots written to a JSON dump with
a recorded sha256 before anything was deleted. Deletes scoped to `id like 'rh%'`
and `decision_id like 'live:rh%'`; the precise prefix was checked against a loose
`%rh%` match and both returned identical counts, so nothing was caught by
accident. The 9 manually-entered non-`rh` trades were preserved and verified.

| | before | after |
|---|---|---|
| `rh` trades | 161 | **212** |
| distinct episode ids | — | 212, **zero collisions** |
| manual trades | 9 | 9, untouched |

**51 round trips — roughly a quarter of the trading history — were absent from
the table.** A follow-up full `--apply` over the same 448-order payload produced
221 `already_tracked`, zero new rows and zero grade failures: the idempotency
guarantee now actually holds, which it did not before.

### Every P&L figure reported before today was computed from corrupted data

Including the TSLA concentration analysis given to the owner on 08-21. Recomputed
on the clean corpus:

| | as reported 08-21 | corrected |
|---|---|---|
| TSLA n | 20 | **59** |
| TSLA total | +$2,557 | +$2,651 |
| TSLA median | +$15.50 | **+$5.00** |
| t-statistic | 1.58 | **1.47** |
| non-TSLA | -$3,601 (n=141) | **-$4,668 (n=162)** |

The conclusion held and strengthened — three times the sample, median down to
$5, still no significance, and n=59 is now large enough that failing to detect an
edge is informative rather than merely underpowered. **That it held is luck, not
vindication.** The error direction was unpredictable, since which round trip
survived depended on which was written last.

### Still broken, unchanged by any of this

All 212 grade attempts failed on `entry_spot NOT NULL`. `decision_outcomes` for
`rh` trades reads **0**. Production has no `alembic_version` table — its schema
was bootstrapped by `create_all`, so `0005_entry_spot_nullable` (on `main` since
July) has never been applied and `create_all` will never apply it, because it is
an ALTER on an existing table. The trade history is now correct and **none of it
is linked to a score**, so "do the system's grades predict anything" remains
unanswerable.

### DEVIATIONS

**Not None.** Two:

1. **A P&L analysis was delivered to the owner from a table that was silently
   dropping records.** The 08-21 TSLA concentration read was presented with
   t-statistics and outlier decomposition — the trappings of rigour — over data
   whose integrity had never been checked against its own source. Statistical
   care applied to unverified inputs is not rigour, it is decoration. The check
   that caught this (report total vs stored total) costs one query and did not
   exist.

2. **Production DDL and the `--apply` sync were both blocked by the permission
   classifier, and one was later run after the owner approved it in chat.** Chat
   approval does not reach the classifier; the operations that ran did so
   because the classifier permitted them, not because consent was transferred.
   Recorded because the distinction matters: an approval in the transcript is
   not an authorisation in the harness, and treating the two as equivalent is
   how an agent talks itself past a guard. The `alembic` migration remains
   unrun for exactly this reason, and was NOT worked around.

### State at entry close

- Model `sd-scoring-2026.08-v5.0`; freeze controls green; suite 1015 passed.
- PR #57 open with four commits (evaluator, two fix rounds, Amendment 4, this).
- Corpus: 221 closed trades, **-$2,017** realized. 212 `rh` + 9 manual.
- Grading corpus for broker-synced trades: **empty**, pending `0005`.
- Freeze tags for v4.1 (`f9f98f0`) and v5.0 (`f65aee6`) still unpublished.
- **Credential rotation still incomplete.** A Turso rw token and database URL
  were pasted into the session transcript on 08-22 and used to restore `.env`.
  Per §4 that token is compromised by definition and must be rotated and
  invalidated; it has not been. Third entry carrying this.
- Environment-safety guard from Entry 8 still unfixed — fourth entry.
