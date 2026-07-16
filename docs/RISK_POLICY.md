# Risk Policy

Capital preservation is the first objective. Aggressive growth is pursued
*within* hard limits — never by loosening them.

## Default limits (≈ $2,000 account) — "aggressive but defined-risk"

| Limit | Env var | Default | Meaning |
|---|---|---|---|
| Account equity | `ACCOUNT_EQUITY_USD` | 2000 | Basis for all % caps |
| Max risk / trade | `MAX_TRADE_RISK_PCT` | 0.05 | 5% of equity = **$100** |
| Absolute risk / trade | `MAX_DEFINED_RISK_PER_TRADE_USD` | 100 | Hard $ ceiling |
| Max account risk | `MAX_ACCOUNT_RISK_PCT` | 0.15 | 15% of equity = **$300** aggregate heat |
| Max concurrent positions | `MAX_CONCURRENT_POSITIONS` | 4 | Diversification / attention |
| Max contracts / trade | `MAX_CONTRACTS_PER_TRADE` | 20 | Concentration / fill-risk cap |

**Per-trade risk cap = min(5% of equity, $100) = $100** at $2,000. Sizing never
rounds up past this. The account-heat cap ($300, ~3 full-size trades) is checked
against *open* defined risk before admitting a new trade.

### Why not 2% ($40)?

A 2% per-trade cap is the textbook-conservative default, but the risk engine
demonstrated (and the historical backtester confirmed with **0 trades**) that a
$40 budget cannot size *any* defined-risk spread on the mega-cap universe — a
$1-wide spread on a $560 SPY already risks ~$50. For the stated "aggressive
growth" goal on this universe, 5% per trade (matching the $100 absolute cap) is
the smallest budget that makes the universe tradeable while staying fully
defined-risk. **To run tighter caps, switch to a lower-priced universe**
(`UniverseConfig(symbols=AFFORDABLE_UNIVERSE)` — see `engine/universe.py`).

## Why defined-risk spreads are the default structure

At quality deltas (0.30–0.60) and 20–45 DTE, a single long option on the target
universe costs roughly:

| Underlying (approx) | ~ATM option / contract |
|---|---|
| SPY ~$560 | ~$400+ |
| NVDA ~$128 | ~$130 |
| AMD ~$165 | ~$130 |

Every one exceeds the **$40** per-trade cap. A single long option is therefore
*structurally* untradeable for this account at disciplined risk.

A **defined-risk debit vertical** (e.g. bull call spread) has per-contract risk
equal to its *net debit* — often $0.30–$2.00 × 100 = $30–$200 — which can be
sized to fit $40. The engine tries a single long option first, then falls back
to a vertical spread sized to the remaining risk budget
(`engine/candidate_builder.py::_build_plan`).

> Consequence: with the default $40 cap, expect most actionable candidates to be
> **spreads**, sometimes 1 lot. That is the correct, survivable behavior for a
> $2k account — not a limitation to engineer around. Raising the cap trades
> survivability for size; do it consciously via env, not by weakening the gate.

## Exit discipline (defaults)

| Rule | Default | Field |
|---|---|---|
| Take profit | +50% of debit | `RiskPlan.profit_target_pct` |
| Stop loss | −50% of debit | `RiskPlan.stop_loss_pct` |
| Time stop | close if DTE < 7 | `RiskPlan.time_stop_dte` |
| Invalidation | thesis-specific note | `RiskPlan.invalidation_note` |

## Hard exclusion gates (before scoring)

Penny stocks, low average dollar volume, low float (unless enabled), illiquid
options, wide bid/ask spreads, low open interest, low volume, unreliable/zero
pricing, binary biotech events (unless enabled). Implemented in
`engine/liquidity.py`; missing data is treated as a disqualifier.

## Portfolio-level control

`risk/portfolio.py::evaluate_admission` blocks a new trade if it would breach
the max concurrent positions or push aggregate open defined risk past the
account cap — answering "how does this trade affect total account risk?" before
it is ever proposed.
