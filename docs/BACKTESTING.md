# Backtesting

Answers platform **Question 14** — "how has this type of setup performed
historically?" — by replaying sized trade plans and aggregating outcomes by
setup type.

## Components (`app/backtest/`)

| Module | Responsibility |
|---|---|
| `app/quant/pricing.py` | The single Black-Scholes model used **everywhere** (mock chain, engine, backtester) so entry, sizing, and repricing are consistent. |
| `pricing.py` | Re-export of the quant model for a stable backtest import path. |
| `engine.py` | Pure per-trade backtest: steps a plan along a daily spot path, reprices with BS, tracks MFE/MAE, applies exits (profit target / stop / time stop / expiry). Reuses the paper engine. |
| `performance.py` | Aggregates results into win rate, expectancy, profit factor, avg MFE/MAE, avg hold — grouped by strategy/direction. |
| `runner.py` | Scan → Monte-Carlo simulate forward paths → backtest each actionable candidate → report. |

## Run it

```bash
python -m app.cli backtest --paths 500          # human-readable
python -m app.cli backtest --paths 500 --json   # machine-readable
curl -X POST 'localhost:8000/backtest?num_paths=500'
```

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
