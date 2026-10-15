# Risk Policy

Capital preservation is the first objective. Aggressive growth is pursued
*within* hard limits — never by loosening them.

## Default limits (≈ $25,000 account) — "aggressive but defined-risk"

Amendment 4 (2026-08-12) raised these. See
`docs/CAPTURE_WINDOW_PREREGISTRATION.md` §8 — the change is a **model change**,
not a config tweak, and carries a `scoring_model_version` bump.

| Limit | Env var | Default | Meaning |
|---|---|---|---|
| Account equity | `ACCOUNT_EQUITY_USD` | 25000 | Basis for all % caps |
| Max risk / trade | `MAX_TRADE_RISK_PCT` | 0.05 | 5% of equity = $1,250 — **does not bind** |
| Absolute risk / trade | `MAX_DEFINED_RISK_PER_TRADE_USD` | 500 | Hard $ ceiling — **this is what binds** |
| Max account risk | `MAX_ACCOUNT_RISK_PCT` | 0.15 | 15% of equity = **$3,750** aggregate heat |
| Max concurrent positions | `MAX_CONCURRENT_POSITIONS` | 4 | Diversification / attention |
| Max contracts / trade | `MAX_CONTRACTS_PER_TRADE` | 20 | Concentration / fill-risk cap |

**Per-trade risk cap = min(5% of equity, $500) = $500.** Sizing never rounds up
past this. The account-heat cap ($3,750) is checked against *open* defined risk
before admitting a new trade.

Which of the two caps binds is load-bearing, not incidental. At $25,000 the
percentage cap is $1,250, so the **absolute** cap governs — per-trade risk is a
fixed $500 and does not drift as the balance moves. If equity ever rose past
$10,000 with the absolute cap left behind, the percentage would silently take
over and "a $500 cap" would stop meaning what it says.
`tests/test_risk_limits_freeze.py::test_the_absolute_cap_is_what_binds` pins this.

Max theoretical deployment is 4 × $500 = **$2,000** against $3,750 of heat, so
the position count is the real constraint and heat never rejects a trade the
per-trade cap allowed. Raising the position count past 7 would change that, and a
test fails if it happens.

## Structure: single legs and spreads, both offered

The short-duration board offers **every** viable defined-risk expression — the
near-ATM single leg *and* the debit vertical — each ranked on its own merits
(`shortduration/contracts.py::select_short_duration_contracts`).

That was always true in code. It was not true on screen. Under the old $100 cap a
near-ATM single leg could not be sized at one contract on any liquid name, so
`build_long_option_plan` returned `None` and only the spread survived:

| Underlying (approx) | ~ATM option / contract |
|---|---|
| SPY ~$770 | ~$250–350 |
| NVDA ~$218 | ~$250+ |
| A $250 name, 2 DTE, 35% IV | ~$261 |

Every one exceeded $100. At $500 they fit, and the board shows both again.

> **The previous version of this document predicted exactly this** — "expect most
> actionable candidates to be **spreads**, sometimes 1 lot" — and called it
> correct behaviour rather than a limitation. It was correct for a $2,000 account.
> It was also reported as a bug on 2026-08-12, because a prediction buried in a
> policy document is not visible on a board. If the cap ever suppresses a
> structure again, say so on the row, not only here.

Raising the cap trades survivability for size. Do it consciously via env, and
expect it to end the capture window: the budget feeds `max_debit_usd` into
contract selection, and the scorer reads `reward_to_risk` off the selected plan.

### Corrections carried in this revision

Two claims in the prior version were wrong and are removed rather than edited:

- It argued against a **2% ($40)** cap that had not been the configured value
  since before v3 — the table said $100 while the prose argued about $40. Flagged
  as a known contradiction in `CLAUDE.md` §9 and left uncorrected because this is
  a governing document; corrected here because the owner has now changed the
  limits deliberately.
- It stated the engine "tries a single long option first, then falls back to a
  vertical spread" and cited `engine/candidate_builder.py::_build_plan`. That
  function does not exist, and the ordering is backwards: `strategy_selector.py`
  attempts `debit_vertical` first at low IV rank and `credit_vertical` first at
  high IV rank. The short-duration path does not rank at all — it returns both.

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
