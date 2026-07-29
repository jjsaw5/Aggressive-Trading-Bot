# Decision Warehouse & Self-Scoring

The platform grades its own suggestions over time. Every actionable decision is
**frozen at the moment it's made**, and later **scored against reality**, so we
can answer the only question that matters for a learning system: *are the
predictions actually any good?*

All of this persists to the durable Turso/libSQL database, so the record grows
across sessions.

## The two records

| Record | When written | What it holds |
|---|---|---|
| `DecisionSnapshot` | at scan time (decision moment) | frozen inputs + prediction: spot, IV, IV rank, composite score, POP, breakevens, expected value, structure economics, full plan |
| `DecisionOutcome` | later (resolution) | ground truth: underlying move, direction correctness, win/loss, realized P&L |

A decision can have several outcomes (e.g. a 21-day check and an at-expiry
check), each labeled by horizon. Snapshots are immutable once written —
warehousing is idempotent and never rewrites a decision after the fact.

- Domain models: `app/domain/outcomes.py`
- Snapshot builder: `app/analytics/snapshots.py` (`snapshot_from_candidate`)
- ORM: `decision_snapshots`, `decision_outcomes` (`app/db/models.py`)

The entry spot is frozen on `SpreadAnalytics.spot_at_analysis` at plan time, so
every decision carries its own reference price — no after-the-fact lookups.

## How an outcome is decided (`app/analytics/outcomes.py`)

Two resolvers, in order of fidelity:

1. **`resolve_from_paper_trade`** — when a simulated position actually closed,
   the realized P&L is the truth. Most accurate.
2. **`resolve_underlying`** — otherwise, score against where the underlying
   finished versus the structure's breakeven(s):
   - bullish singles (long call, bull call, bull put) win **above** breakeven;
   - bearish singles (long put, bear put, bear call) win **below** breakeven;
   - long straddle/strangle win **outside** the wings;
   - iron condor wins **inside** the wings;
   - a small band around breakeven is a **scratch**.

   This is an **intrinsic-at-horizon proxy** — exactly right at expiry, a
   reasonable directional read before then. It is labeled as such
   (`outcome_source="underlying_vs_breakeven"`) and uses only underlying prices,
   because no historical option-quote feed is wired (see the
   `HistoricalOptionsProvider` slot). It's the honest best we can do from prices
   alone, and it is precisely what probability-of-profit is defined against
   (finishing past breakeven).

`direction_correct` is tracked separately from win/loss and is only set for
directional theses (bullish/bearish); neutral and vol structures leave it null.

## Two settlements, because there are two questions

An expired decision is graded twice, on purpose, and the two grades answer
different questions. Conflating them is how a book gets misjudged.

| Grade | Module / runner | Answers |
|---|---|---|
| **Hold-to-expiry** (`expiry_settlement`) | `scripts/settle_pending_decisions.py` | "Did it finish past breakeven?" — exactly what probability-of-profit claims. At expiration a defined-risk structure has no extrinsic value left, so signed intrinsic **is** the payoff; nothing is modelled. |
| **Managed policy** (`managed_policy`) | `scripts/settle_under_policy.py` | "Would the strategy have made money?" — the plan's own profit target, stop, and DTE time-stop replayed over real daily option marks from entry forward. |

The app never holds to expiry: its plans take profit at 40-60%, stop at -50%,
and time-stop by DTE regime. So the scorecard **splits the sources by metric** —
win rate and Brier read the hold-to-expiry grade (that is what POP forecasts),
while every dollar metric (P&L, score↔P&L Spearman, drawdown) reads the managed
replay when one exists (`calibration.select_pnl_outcomes`). Both appear as
scorecard warnings so the reader always knows which policy produced which number.

Fidelity discipline in the managed replay:

- the stop is checked **before** the profit target, so a day that traded through
  both books the loss — assuming the good fill on an ambiguous bar is how a
  backtest flatters itself;
- a day where any leg is unpriced is **held through**, never valued on a partial
  structure;
- a decision the feed cannot price at all is **abstained** (no outcome written),
  because a policy grade built on a guessed path is worse than no grade;
- marks dated before entry can never trigger an exit.

`DecisionOutcome.exit_reason` carries `profit_target | stop_loss | time_stop |
expiry` for policy-replayed grades and is empty for grades that never simulated a
path.

## The scorecard (`app/analytics/calibration.py`)

`build_scorecard` pairs each decision with its best available outcome (paper
trade preferred, else the longest-horizon underlying resolution) and reports:

- **Win rate** over decisive outcomes (scratches/unknowns excluded).
- **Direction accuracy** for directional theses.
- **POP calibration** — decisions bucketed by predicted probability of profit vs
  the realized win rate in each bucket. A well-calibrated 70% bucket wins ~70%.
- **Brier score** — mean squared error of the POP forecast (lower is better).
- **Score calibration** — is the composite score monotonic in realized win rate?
- Breakdowns **by strategy** and **by direction**.

## API (`/outcomes`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/outcomes/snapshots?status=pending\|resolved` | browse warehoused decisions |
| POST | `/outcomes/resolve?min_age_days=&at_expiry_only=` | resolve matured decisions against current prices |
| GET | `/outcomes/calibration` | the self-scoring scorecard |
| GET | `/outcomes/{decision_id}` | one decision + all its outcomes |

## Wiring

- **Capture** happens automatically: `POST /scans` and the scheduler warehouse
  every actionable candidate (`warehouse_candidates`).
- **Resolution** runs in the scheduler each cycle (`resolve_pending`, matured
  decisions only) and on demand via `POST /outcomes/resolve`. It fetches current
  underlying quotes through the provider abstraction — no new data source.

## Honesty notes

- Underlying-vs-breakeven outcomes approximate intrinsic value at the horizon;
  they are not marked option exits. Paper-trade outcomes use real realized P&L.
- Win rate excludes scratches and undetermined outcomes; `n_decisive` shows the
  denominator so a thin sample is never mistaken for a strong one.
- Everything is stored, nothing is overwritten — the warehouse is an audit trail.
