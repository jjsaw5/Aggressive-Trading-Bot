# Backtesting

Answers platform **Question 14** — "how has this type of setup performed
historically?" — by replaying sized trade plans and aggregating outcomes by
setup type.

## Two modes

| Mode | Paths | Use |
|---|---|---|
| **Simulated** | zero-drift GBM | validate structure/exits with no assumed edge (baseline sanity) |
| **Historical** | **real underlying price history** | measure how the strategy actually performed on market data |

## Components (`app/backtest/`)

| Module | Responsibility |
|---|---|
| `app/quant/pricing.py` | The single Black-Scholes model used **everywhere** (mock chain, engine, backtester) so entry, sizing, and repricing are consistent. |
| `pricing.py` | Re-export of the quant model for a stable backtest import path. |
| `engine.py` | Pure per-trade backtest: steps a plan along a daily spot path, reprices with BS, tracks MFE/MAE, applies exits (profit target / stop / time stop / expiry). Reuses the paper engine. |
| `historical.py` | Replays **real** underlying candles: walks historical entry dates, reconstructs the setup *as of each date* (trailing trend → direction, trailing realized vol → pricing), sizes a defined-risk vertical, backtests over the actual forward path. No look-ahead. |
| `performance.py` | Aggregates results into win rate, expectancy, profit factor, avg MFE/MAE, avg hold — grouped by strategy/direction. |
| `runner.py` | `run_backtest` (simulated) and `run_historical_backtest` (real paths via the market-data provider). |

## Run it

```bash
# Simulated (structure/exit sanity, zero assumed edge)
python -m app.cli backtest --paths 500
curl -X POST 'localhost:8000/backtest?num_paths=500'

# Historical — REAL underlying paths (real market data when FMP/Robinhood set)
python -m app.cli backtest --historical
curl -X POST 'localhost:8000/backtest/historical?lookback_days=365'
```

## Historical mode — how "real" it is

- **Underlying path: real.** Sourced through `MarketDataProvider.get_price_history`
  — actual EOD candles from FMP (`/stable/historical-price-eod/full`) or
  Robinhood when configured; the deterministic mock history otherwise. The
  underlying path is the dominant driver of a directional spread's P&L, so this
  is the substantive upgrade from simulation.
- **Option legs: repriced with Black-Scholes at trailing realized vol.** This is
  the remaining approximation. The clean upgrade is a `HistoricalOptionsProvider`
  (interface already defined in `providers/base.py`) supplying recorded
  per-contract marks — the engine already accepts explicit marks via the path.
- **Strategy under test:** trend-following (SMA-fast vs SMA-slow, causal)
  defined-risk verticals, sized by the live risk policy. No look-ahead: entry
  direction/vol use only data up to the entry index.

> **Affordability note:** the per-trade cap is `min(equity × max_trade_risk_pct,
> max_defined_risk_per_trade_usd)` — by default `min($2k × 5%, $100) = $100`. A
> tighter cap (e.g. the older 2% → $40) cannot size a $1-wide spread on a $560
> name (~$50), so historical mode returns **0 trades** there. Set a per-trade
> budget matching the universe (see `docs/RISK_POLICY.md`). This is the risk
> engine correctly reporting that the universe is unaffordable at that cap.

## Why the harness is trustworthy (and its limits)

The runner simulates forward underlying paths with a **zero-excess-drift GBM**
(risk-free rate, implied vol) and reprices the entry on the *same* BS curve the
path uses. Under these assumptions a fairly-priced defined-risk spread must have
**~zero expectancy minus costs** — and that is exactly what the harness reports:

```
OVERALL  win ~48% | expectancy ~$0 | PF ~1.0 | MFE ≈ -MAE
```

That near-zero baseline is the point: it proves the backtester is **not
manufacturing fake alpha**. Any genuine edge — a correct directional thesis
(positive drift), an IV mispricing (realized ≠ implied), or a superior exit
rule — then shows up as expectancy *above* this baseline.

### Important limitations
- **Simulated paths, not real option history.** A historical *options* data
  vendor is not yet wired. Treat aggregate numbers as behavioral/structural
  indicators, **not** expected returns.
- Constant IV, no early assignment, no dividends, European-style pricing,
  daily (not intraday) exit monitoring.
- Entry sizing comes from the live plan; the sim reprices the entry mark for
  consistency, so tiny differences vs. the stored debit are expected.

## Making it production-grade (next)

Feed the pure `engine.py` **real historical option marks** (the engine already
takes an explicit path and is model-agnostic at the mark level) and replay
**stored scans** rather than fresh simulations. Then group `performance.py` by
score bucket and IV-rank bucket to learn which setups actually pay.
