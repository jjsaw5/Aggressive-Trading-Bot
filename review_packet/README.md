# Post-Remediation Audit Packet

Assembled 2026-08-01 at `a517377`. **Start with `status_matrix.md`.**

## Provenance of every artifact here

| File | How produced |
|---|---|
| `status_matrix.md` | The index. Read first. |
| `FINDING_01_dormant_scoring_input.md` | **HIGH severity.** Surfaced by this packet's own checks; not previously known. |
| `sample_export_post_fix.csv` | 6 signals × 108 cols. Merged code, **live FMP/UW providers**, every DB write stubbed. Not production output — see below. |
| `evidence_1_acceptance_checks.txt` | The reviewer's stated criteria run against that CSV, pass and fail alike. |
| `evidence_1_4_marks_sample.csv` | 123 real minute bars, `SPY260728C00730000`, 2026-07-28, pulled live. |
| `evidence_0_5_prereg.md` | Commit hash, ISO date, and the full committed text. |
| `evidence_4_freeze_diff.txt` | `git diff` over scoring paths — empty. |
| `evidence_7_*` | Test output: 783 full suite, 182 named invariant tests, 0 failures. |

## The one thing that most limits this packet

**Production has not been redeployed.** PR #48 merged Phases 0–2 to `main` about
an hour before assembly; the running container is still on `7afa098`. No signal
the deployed system has ever produced carries capture gates, market context, or
intraday grading.

So wherever the request asked for "signals generated AFTER Phase 1 deployment"
or "new resolved rows", no production data exists. Where a sample was still
producible I generated it by running the merged code against live providers with
persistence stubbed. That demonstrates the pipeline works. It does not
demonstrate that production behaved, and it is labelled that way throughout.

One trading session after redeploy converts every "no production rows" item into
a real sample.

## What is not here, and why

- **`docs/audit/SESSION_LOG.md` does not exist.** Never created. Every "log ref"
  column reads `n/a`. This is a genuine gap in the audit trail.
- **Phases 3 and 4 were never started** — 8 of 10 items `NOT_STARTED`.
- **No CI exists at all** (no `.github/` directory), so item 0.2 is `BLOCKED`
  rather than partially met.
- **The data dictionary was not updated** for the ~30 new columns.

## Corrections to my own prior reporting

1. **Item 1.3 was reported complete. It is 3 of 4** — `term_slope` is plumbed but
   never populated in production. See `FINDING_01`.
2. **Item 1.4 was reported delivered as specified. It diverges**: on-demand fetch,
   no poller, no stored mark series. See `evidence_1_4_cadence_note.txt`.
3. **Item 1.11 was reported delivered. It diverges**: a per-signal vol×tape tag,
   not the requested daily VIX/SPX regime table.

## Caveats a reviewer should carry into the numbers

- **Greeks are modeled, not measured.** No provider in the stack supplies them.
  Every row is stamped `black_scholes_modeled`.
- **MFE/MAE are bounds, not achieved prices.** A minute bar's high and low have
  no ordering.
- **Minute bars are trade-driven and sparse** — 31% session coverage on a liquid
  0DTE contract, largest gap 53 minutes. The replay holds through gaps and never
  interpolates, so a missed exit is *missed*, not mispriced. Bias direction:
  trades look like they ran longer than they did.
- **Cost stress is execution-derived**, not quote-derived
  (`cost_stress_source=effective_from_side_volume`).
