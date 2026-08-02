# Status matrix — Review Packet #2

Generated 2026-08-01. Repository at `34d0bad` + this session's commit.
Production at `7afa098`.

**Read this first.** Nine of eleven pre-flight items are closed with artifacts.
The two that are not are the two that convert everything else into a real
result, and both are yours to do.

---

## Section A — Pre-flight closure

| # | Item | Status | Evidence |
|---|---|---|---|
| P1 | Golden file pins the scorer | **DONE** | `A_P1_golden_file.txt`, `A_P1_freeze_controls_verbose.txt` — 8 cases, 20 tests |
| P1a | *The failure log Ruling 1 predicted* | **DOES NOT EXIST** | `A_P1_golden_file.txt` — structurally impossible; equivalent proof supplied |
| P2 | FINDING_01 closed, v3 → v3.1 | **DONE** | `A_P2_finding01.txt` — fix, version bump, Amendment 1, contract test |
| P3 | CI + freeze path guard | **DONE** | `A_P3_ci_pathguard.txt` — passing run on main + PR #52 failing verbatim |
| P4 | Freeze tag published | **DONE** | `A_P4_freeze_tag.txt` — `80eb42c…`, plus `docs/FREEZE_POINT.md` |
| P5 | Data dictionary complete | **DONE** | `A_P5_data_dictionary.txt` — 90 entries / 86 columns |
| P6 | Daily regime table | **DONE** | `A_P6_regime_table.txt` — 395 rows live, 2025-01-02..2026-07-31 |
| P7 | Mark quality + 0DTE bar | **DONE** | `A_P7_mark_quality.txt` — 20 tests; gate states 80% / 5min / 52min |
| P8 | Governance committed | **DONE** | `A_P8_governance.txt` — root cause determined, not guessed |
| P9 | Branch protection | **NOT_STARTED** | `A_P9_P10_P11_blocked.txt` — nothing done; needs repo settings |
| P10 | Redeploy production | **NOT_DONE** | `A_P9_P10_P11_blocked.txt` — still `7afa098` |
| P11 | Verify production behaved | **BLOCKED on P10** | `A_P9_P10_P11_blocked.txt` — 0 signals under v3.1, verified |
| — | Test suite & lint | **853 passed** | `A_test_suite.txt` — includes a ruff-scope gap I found and fixed |

### The three that are not closed

**P9 is not started.** No branch-protection rule exists. `freeze-guard` fired
correctly on PR #52, but nothing *requires* it, and a direct push to main skips
it (it is `pull_request`-only and shows `skipped` on push). The control is
demonstrated and advisory. **P9 should land before P10**, not after — once the
window opens, an undeclared change contaminates a live corpus instead of an
empty one.

**P10 is not done.** Production runs `7afa098`. Phases 0–2 and P1–P8 exist only
in the repository. No deployed signal has ever carried capture gates, market
context, or intraday grading.

**P11 cannot start.** Verified against the live warehouse today:

```
total decision snapshots          : 40154
under sd-scoring-2026.08-v3.1     : 0
market_context PRESENT            : 0
newest: 2026-07-31 21:23:25  QQQ  version='sd-scoring-2026.07-v3'  mktctx=None
```

Your note was *"P11 is the step that converts 'the pipeline works' into
'production behaved'."* It is the gating item and it is at zero.

### One item reported as impossible rather than satisfied

Ruling 1 step 2 predicted the golden file would break on the term_slope fix. It
does not and cannot: it scores fixed `IVContext` fixtures, so a *provider*
change cannot move its numbers. I did not reshape the test to manufacture the
predicted failure. The equivalent proof exists and is real — the provider
contract test went 8-of-9-failing → 9-of-9-passing across that commit
(`review_packet/evidence_R1_proof_of_efficacy.txt`), and the freeze guard caught
the version bump independently.

---

## Section B — Front-end review

Centre of gravity: **display honesty.** 12 screenshots, all byte-distinct,
filenames verified against what they actually show.

| # | Item | Status | Evidence |
|---|---|---|---|
| B1 | Orientation | **DONE** | `B1_orientation.md` — 1 file, 2,027 lines, 18 views |
| B2 | Screens & states | **DONE** | `B2_01..12*.png`, `B2_console_errors.txt` |
| B3 | Claims inventory | **DONE** | `B3_claims_inventory.md` |
| B4 | Execution affordances | **DONE — they exist** | `B4_execution_affordances.md` |
| B5 | Data contract + client math | **DONE** | `B5_data_contract.md`, `B5_confidence_is_derived.txt`, 5 `.json` |
| B6 | Source | **DONE** | `B6_source_notes.md` |
| B7 | The four questions | **DONE** | `B7_answers.md` |

### Verdict in one line

**The front end calculates in prose and recommends in pixels.**

### What holds

- The truth banner renders on **every** SD tab — *"not a trade signal … no
  feature has shown net-of-cost edge … The thesis is yours."*
- Nothing overstates in text, anywhere in 2,027 lines.
- UNCALIBRATED **fails closed**: missing scorecard, unexpected string, or gate
  exception all read UNCALIBRATED. The `CALIBRATED` branch cannot render today.
- Absent stays absent end-to-end: `ABST` for abstained rows, `null` for every
  calibration statistic, `[]` for the suspended 0DTE bucket, `"insufficient"`
  for the flow verdict.
- The execution double-gate is real, tested, and has no broker call in any
  branch.
- The honest parts are the parts the **server** computed; the problems are all
  parts the **browser** decided.

### The two findings

**1. `PICK #1` badges.** (`dashboard.html:972`, plus a `pickrow` row highlight.)
A blue badge, an ordinal, and visual promotion — recommendation in three
registers — about sixty lines from a banner saying the product does not
recommend. Both are rendered by the same call stack. A calculator does not have
picks. This is not a wording fix: the ordinal, the badge and the highlight class
all have to come out together. Keep `engine_pick` as captured data; stop
blue-badging it `#1`.

**2. `confidence NN%` manufactures corroboration.** Presented as a peer of
`tradability` and `data quality`. It is neither independent nor a probability:

```
engine.py:108   overall = round(normalized * (0.6 + 0.4 * data_quality), 4)
```

Reproduced **exactly on 58 of 58 live rows, residual 0**
(`B5_confidence_is_derived.txt`). A reader seeing 77 and 72% agree believes a
second instrument corroborates the first. There is no second instrument, and the
`%` invites reading a rank-derived scalar as a likelihood.

### Also raised

| Finding | Where |
|---|---|
| 11 timestamps are browser-local, not ET — a correctness hazard on a 0DTE product (the codebase pins `timeZone` exactly once, line 1562) | `B5` |
| POP colour bands (0.40 / 0.55) and cost-drag bands (0.15 / 0.30) invented in the stylesheet, unversioned, and **duplicated** at lines 657 and 1022 | `B5` |
| *"only ever routes an order when live trading is explicitly armed"* describes a switch; there is no broker path in this build at all | `B4` |
| CI lints `app/ scripts/ tests/` only — `alembic/` is outside it, and had a real error | `A_test_suite.txt` |

### Recommended order

1. Remove `PICK #N` (badge + ordinal + `pickrow`)
2. Drop the standalone `confidence %`, or render it as `score × data quality`
3. Pin `America/New_York` in one shared formatter, label it `ET`
4. Serve colour thresholds from config; de-duplicate 657/1022
5. Soften the double-gate prose to match the build
6. Widen CI's ruff scope

Items 1–3 are worth doing **before P10**. Once production redeploys, every
screen read during the capture window is one of these.

---

## STANDING AND OVERDUE — credential rotation

Still not done, and it is the oldest open item in this project:

- **Unusual Whales API key** — exposed in a session transcript
- **Turso read-write token** — exposed
- **Turso read-only JWT** — exposed

All three remain live, against the production Turso database. Rotate all three.
This does not become less urgent by being repeated.
