"""Backtesting harness — answers "how has this setup performed historically?".

Design:
  * `pricing.py`   — Black-Scholes repricing of a TradePlan's legs through time.
  * `engine.py`    — pure per-trade backtest: step a plan along a price path,
                     track MFE/MAE and apply the plan's exit rules (reuses the
                     paper engine), return a closed PaperTrade + trajectory.
  * `performance.py` — aggregate closed trades into stats grouped by setup type
                     (strategy/direction/score bucket): win rate, expectancy,
                     profit factor, avg MFE/MAE, avg hold.
  * `runner.py`    — orchestrate a scan -> backtest each actionable candidate
                     over a (currently simulated) forward path -> report.

NOTE: a historical *options* data vendor is not yet wired. The engine is pure
and takes an explicit underlying price path, so it is exact and fully testable.
The runner SIMULATES forward paths (seeded GBM) and labels results as such —
swap in real historical option marks to get production-grade backtests.
"""
