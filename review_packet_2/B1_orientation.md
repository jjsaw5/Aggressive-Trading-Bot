# B1 — Orientation

## What the front end is

One file. `app/web/dashboard.html`, 2,027 lines. Vanilla JS, no framework, no
build step, no bundler, no npm. Served whole by
`app/api/routes/dashboard.py:17`. All rendering is template literals; all state
is module-scope variables in one `<script>` block.

This matters for the review: **there is no component boundary anywhere.** Every
label, threshold and colour decision lives inline at its point of use, so a
claim made in one view is not structurally shared with another. Where two views
say the same thing, they say it twice.

## Navigation

Hash-routed. View ids are `today` | `core/<tab>` | `sd/<tab>`, driven through
`setView()` at line 1739; the hash is written with `history.replaceState` so
every view is linkable and reloadable.

| Group | Views |
|---|---|
| landing | `today` |
| core | `candidates` `positions` `proposals` `paper` `calibration` `realmark` `ops` `history` |
| short-duration | `command` (0DTE) `scanner` (1–5DTE) `medium` `candidates` `positions` `news` `events` `performance` `config` |

18 view ids in total.

## Screens captured (B2)

12 PNGs, all verified byte-distinct by md5 and filename-checked against what
they actually show — a first batch was discarded after three files hashed
identically, which proved my route guesses (`#/scanner`) were wrong; the real
scheme is `#sd/scanner`.

| File | Screen | State |
|---|---|---|
| `B2_01_today_landing.png` | Today | landing, populated |
| `B2_02_sd_scanner_1_5dte_populated.png` | 1–5DTE Scanner | populated, 58 rows |
| `B2_03_sd_trade_candidates_all_boards.png` | Trade Candidates | all boards |
| `B2_04_sd_medium_duration.png` | Medium Duration | populated |
| `B2_05_sd_0dte_command_center_suspended.png` | 0DTE Command Center | **suspended** |
| `B2_06_sd_performance.png` | Performance | no resolved decisions |
| `B2_07_sd_positions.png` | SD Open Positions | empty |
| `B2_08_core_proposals_execution.png` | Proposals | **execution affordances visible** |
| `B2_09_core_calibration.png` | Calibration | 0 resolved |
| `B2_10_core_positions.png` | Positions | populated |
| `B2_11_core_history.png` | History | populated |
| `B2_12_core_candidates_empty.png` | Core Candidates | **empty state** |

`B2_console_errors.txt` — one 404 on a resource fetch across the whole sweep.
No JS exceptions.

## How the screenshots were produced, and what that limits

Local instance on `127.0.0.1:8099` against a temporary SQLite file, **all
providers set to mock**, Turso blanked. 78 candidates seeded via
`run_detection(DTECategory.SHORT_DTE)`.

Two honest consequences:

1. **The numbers on these screens are mock-derived.** They are correct as
   *renderings* — the layout, labelling, colour and claim text are exactly what
   production renders — but no price on them is real. This review is about how
   the UI *presents* a number, which mock data serves; it is not evidence about
   what production would show.
2. **Production could not be screenshotted anyway.** Production still runs
   build `7afa098` (P10, not done), so its UI is the *previous* front end. The
   screens here are of the code under review, which has never been deployed.
