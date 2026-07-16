# Aggressive Trading Bot

An options-trading **research and decision-support** platform for a small,
aggressive account (initial size ≈ $2,000). It continuously evaluates a
configurable universe of liquid equities/ETFs and their options, and produces
**ranked, fully-explained trade candidates** with **defined-risk trade plans**.

> **It does not place live trades.** Automation is disabled by default behind a
> double kill-switch. The platform's job is research, ranking, risk planning,
> paper trading, and human-approval tickets — capital preservation and
> traceability first.

---

## What it answers

For every symbol, a scan answers the 14 platform questions:

| # | Question | Where it's answered |
|---|----------|---------------------|
| 1 | What symbols are worth monitoring? | `engine/universe.py`, liquidity gates |
| 2 | Why is the opportunity appearing now? | `engine/flow.py` → `Thesis.why_now` |
| 3 | Bullish / bearish / neutral / vol? | `engine/scoring.py::resolve_direction` |
| 4 | Is the options flow meaningful or noise? | `engine/flow.py` (premium/sweep/opening) |
| 5 | Is price action confirming the flow? | `engine/price_action.py` + confirmation multiplier |
| 6 | Is there a catalyst? | `engine/catalysts.py` |
| 7 | Is implied volatility favorable? | `engine/volatility.py` (direction-aware) |
| 8 | Which contract/spread expresses the thesis? | `engine/contract_selection.py` |
| 9 | What is the maximum defined risk? | `risk/trade_plan.py` → `RiskPlan.max_loss_usd` |
| 10 | What invalidates the trade? | `RiskPlan.invalidation_note` |
| 11 | When to take profits? | `RiskPlan.profit_target_pct` |
| 12 | When to close? | `stop_loss_pct` / `time_stop_dte` |
| 13 | How does it affect total account risk? | `risk/portfolio.py::evaluate_admission` |
| 14 | How has this setup performed historically? | backtest harness (roadmap) |

## Operating modes

1. **Research only** (default) — ranked candidates + explanations. No orders.
2. **Paper trading** — simulated fills, slippage, MFE/MAE, exit performance.
3. **Human approval** — proposal tickets requiring explicit approval.
4. **Limited automation** — narrow, per-strategy, **disabled by default** and
   gated by the execution guard. Unrestricted autonomous trading is not
   representable by the API.

## Key design decision: defined-risk spreads for a small account

A concrete finding from the risk engine: at quality deltas (0.30–0.60), a
single long option on this mega-cap universe costs **$130–$450 per contract**,
so the engine prefers **defined-risk debit vertical spreads**, whose
per-contract risk is the (much smaller) net debit. A second finding followed:
even spreads on $500+ names cannot be sized under a 2% ($40) per-trade cap, so
the default risk profile is **"aggressive but defined-risk"** — 5% ($100) per
trade, 15% account heat, matching the $100 absolute cap. Run tighter caps only
with a lower-priced universe (`AFFORDABLE_UNIVERSE`). See
[`docs/RISK_POLICY.md`](docs/RISK_POLICY.md).

## Architecture at a glance

```
providers/           external data behind capability interfaces (swappable)
  base.py            MarketData / Fundamentals / OptionsChain / OptionsFlow /
                     Calendar / Brokerage  + ProviderMeta (auth/delay/limits/license)
  mock/              deterministic synthetic provider (runs with zero keys)
  fmp/ unusual_whales/ robinhood/   grounded live clients
engine/              universe → liquidity gate → signals → scoring → contract → candidate
risk/                policy · position sizing · portfolio heat · trade plans
modes/               execution guard (kill-switch) · proposal lifecycle
services/            scan orchestration · paper engine (MFE/MAE) · store
api/                 FastAPI routes (health, config, scans, proposals)
db/ alembic/         SQLAlchemy models + migrations
scheduler/           periodic research scans (APScheduler)
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full picture and
[`docs/ROADMAP.md`](docs/ROADMAP.md) for what's built vs. next.

## Quick start (no API keys required)

The default provider stack is the deterministic **mock**, so the whole pipeline
runs immediately:

```bash
make install          # pip install -e ".[dev]"
make test             # 31 tests, all green
python -m app.cli scan            # ranked candidates in the terminal
python -m app.cli providers       # configured provider status
make run                          # FastAPI at http://localhost:8000/docs
```

Example API flow:

```bash
curl -s -X POST 'localhost:8000/scans?actionable_only=true' -H 'content-type: application/json' -d '{}'
curl -s localhost:8000/config/providers        # data-source status + licensing notes
```

## Full stack with Docker

```bash
cp .env.example .env      # fill in provider keys to go beyond mock
make up                   # api + scheduler + postgres + redis
```

## Enabling real data providers

Each integration was researched against **current official documentation** and
lives behind the provider abstraction. Before switching a capability off
`mock`, read the corresponding provider doc — it records confirmed endpoints,
auth, rate limits, data delay, tier, and licensing:

- [`docs/providers/FINANCIAL_MODELING_PREP.md`](docs/providers/FINANCIAL_MODELING_PREP.md) — market data, fundamentals, calendar (no options)
- [`docs/providers/UNUSUAL_WHALES.md`](docs/providers/UNUSUAL_WHALES.md) — options flow + IV-rank history
- [`docs/providers/ROBINHOOD.md`](docs/providers/ROBINHOOD.md) — option chains + greeks/IV, quotes, account (via robin_stocks; `[robinhood]` extra)

Set the `PROVIDER_*` variables in `.env` to route each capability. A misrouted
or unbuilt provider fails loudly at resolution time — it never silently returns
bad data.

## Safety model

- Automation requires **both** `TRADING_MODE=automation` **and**
  `AUTOMATION_ENABLED=true`; default is neither.
- Every live-order intent must pass `modes/execution_guard.py`, which also
  requires a human-**approved** proposal. Approval is never implied.
- Missing/zero market data is treated as a **disqualifier**, never an
  optimistic assumption.

## Status

This is an initial build: architecture, provider abstraction, mock stack,
signal/scoring engine, risk engine (sizing + portfolio + defined-risk spreads),
paper engine, approval workflow, execution guard, API, persistence models, and
tests are in place. Live provider clients are grounded but require key + field
validation before production use. See the roadmap for the sequenced next steps
(backtesting, IV-rank sourcing, spread analytics, dashboard).

**Not financial advice. Options trading involves substantial risk of loss.**
