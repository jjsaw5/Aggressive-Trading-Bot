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

### Spread analytics & more structures — built
The engine selects among long options, debit/credit verticals, straddles,
strangles, and iron condors, choosing debit vs credit by IV rank (and vol
structures for neutral theses with the right IV/catalyst setup). Every plan
carries computed analytics — probability of profit, net greeks, breakeven(s),
is_credit — via `app/quant/analytics.py`. See [`docs/STRATEGIES.md`](STRATEGIES.md).
Credit-aware exits are now built too: the paper engine and backtester price by
signed net and manage credit structures at a % of credit captured, so iron
condors and credit verticals are scored end-to-end.

### Real IV-rank / IV-percentile sourcing — built
IV rank and percentile are now **computed** (`app/quant/iv.py`) from a real IV
history rather than an opaque provider field. Source priority: a real
`IVHistoryProvider` (Unusual Whales, grounded on the confirmed
`GET /api/stock/{ticker}/iv-rank`) → true IV rank; else a realized-volatility
proxy from real price history (labeled `hv_proxy`, lower signal confidence). The
`IVContext.iv_rank_source` tag makes every rank auditable, and the mock's price
path was made vol-coherent with its option IV. `PROVIDER_IV_HISTORY` routes it.

### Robinhood provider — built (pending live verification)
Real options chains + greeks/IV, quotes, historicals, account equity, and open
option positions via robin_stocks 3.4.0 (grounded on the library source). Lazy
threaded session with headless pyotp MFA; pure, unit-tested response mapping.
`meta.verified=False` until a live auth + field smoke test is run against a real
account. Order placement intentionally omitted. See the provider doc.

## Next (sequenced)

### 1. Live-verify Robinhood + real end-to-end run
Smoke-test auth/MFA and field mapping against a real account; confirm quote
freshness and option-chain greeks/IV populate; then set `meta.verified=True`.

### 2. Persistence repository + history API
Replace the in-memory store with the DB repository (models/migrations exist);
add scan history, candidate/proposal history, and paper-trade P&L endpoints.

### 3. Alerts
Push notable candidates/flow to a channel (email/Slack) behind the same
provider-style abstraction.

### 4. Dashboard
Next.js/React front end over the API: ranked candidates, thesis breakdown,
risk plan, paper P&L, provider/licensing status.

## Guardrails that must never regress
- Automation stays off by default; the double-gate + approval requirement stays.
- Sizing never exceeds the per-trade or account caps.
- No fabricated endpoints or response fields; new integrations update their
  provider doc and `ProviderMeta.verified`.
