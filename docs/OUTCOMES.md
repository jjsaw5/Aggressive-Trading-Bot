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
