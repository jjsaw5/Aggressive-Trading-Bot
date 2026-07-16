# Roadmap

## Built in this initial phase

- Modular architecture with a strict **provider abstraction** (capability
  interfaces + registry + `ProviderMeta`).
- **Deterministic mock provider** — full pipeline runs with zero keys.
- **Grounded live clients** (endpoints/auth verified vs official docs):
  FMP (market/fundamentals/calendar), Unusual Whales (options flow). Robinhood
  skeleton (options chain/brokerage) pending build.
- **Signal engine**: options flow, price action (SMA/RSI), direction-aware IV,
  catalysts; composite scoring with a flow×price **confirmation multiplier**.
- **Liquidity/quality gates** implementing all requested exclusions.
- **Risk engine**: policy, defined-risk position sizing (caps never breached),
  portfolio heat/admission, long-option **and defined-risk vertical spread**
  trade plans with entry/exit/invalidation.
- **Paper engine**: simulated fills + slippage, MFE/MAE, exit rules.
- **Modes**: research + paper + approval proposals + **execution guard**
  (automation off by default, approval always required).
- **FastAPI** (health, runtime/provider config, scans, proposal lifecycle),
  **SQLAlchemy models + Alembic**, **APScheduler** periodic scans, CLI.
- **Tests** (31) + ruff-clean, Docker/Compose.

## Recently added

### Backtesting harness (Question 14) — built
Shared Black-Scholes model (`app/quant/pricing.py`), a pure per-trade backtest
engine (MFE/MAE + exit rules, reuses the paper engine), a performance aggregator
(win rate / expectancy / profit factor by setup type), a Monte-Carlo runner, CLI
`backtest`, and a `/backtest` endpoint. Validated unbiased: a zero-drift sim
reports ~zero expectancy, so real edge shows up above that baseline. See
[`docs/BACKTESTING.md`](BACKTESTING.md). **Next:** feed real historical option
marks and replay stored scans (the engine already takes explicit paths).

## Next (sequenced)

### 1. Real IV-rank / IV-percentile sourcing
`IVContext.iv_rank` currently comes from the provider. Wire a real source
(Unusual Whales greek/vol endpoints or a computed 1-year IV history) and
validate the direction-aware volatility scorer against it.

### 2. Build & verify the Robinhood provider
Options chains + Greeks + account state via a verified `robin_stocks` surface
(or alternative). Confirm ToS, auth/MFA, and field mapping. See the provider doc.

### 3. Spread analytics & more structures
Add credit spreads, straddles/strangles (for the vol-long/vol-short directions
already modeled), and iron condors; richer greeks-based selection and
probability-of-profit estimates.

### 4. Persistence repository + history API
Replace the in-memory store with the DB repository (models/migrations exist);
add scan history, candidate/proposal history, and paper-trade P&L endpoints.

### 5. Alerts
Push notable candidates/flow to a channel (email/Slack) behind the same
provider-style abstraction.

### 6. Dashboard
Next.js/React front end over the API: ranked candidates, thesis breakdown,
risk plan, paper P&L, provider/licensing status.

## Guardrails that must never regress
- Automation stays off by default; the double-gate + approval requirement stays.
- Sizing never exceeds the per-trade or account caps.
- No fabricated endpoints or response fields; new integrations update their
  provider doc and `ProviderMeta.verified`.
