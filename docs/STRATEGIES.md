# Strategies & Spread Analytics

The engine now selects among several defined-risk structures and picks the one
that best fits the thesis **and the implied-volatility regime**, then attaches
full analytics (probability of profit, net greeks, breakevens) to every plan.

## Structures

| Strategy | Type | Stance | Max loss | Max profit |
|---|---|---|---|---|
| Long call / put | debit | directional | debit | uncapped |
| Bull call / bear put spread | debit | directional | debit | width − debit |
| Bull put / bear call spread | **credit** | directional | width − credit | credit |
| Long straddle | debit | long vol | debit | uncapped |
| Long strangle | debit | long vol | debit | uncapped |
| Iron condor | **credit** | short vol | wider wing − credit | credit |

All are **defined-risk** (bounded max loss), sized so worst-case loss respects
the per-trade and account caps (`app/risk/position_sizing.py`).

## Selection logic (`app/engine/strategy_selector.py`)

Let IV rank pick debit vs credit — the key link to the IV-rank feature:

```
Directional + LOW IV rank   -> debit vertical (cheap premium, convex)   then long option
Directional + HIGH IV rank  -> credit vertical (rich premium, high POP)  then debit
Neutral    + HIGH IV rank   -> iron condor (defined-risk short vol)
Neutral    + LOW IV + catalyst -> long strangle / straddle (long vol into event)
```

Structures are attempted in priority order; the first that **sizes within the
risk policy** wins. On the mega-cap universe at a tight per-trade cap, credit
spreads/condors often can't be sized (they'd need ~$1-wide strikes), so the
selector falls back to debit verticals — the same affordability reality as
elsewhere. On lower-priced names or larger budgets the full menu is used.

Example (spot 100, roomy budget):

| Regime | Chosen | POP | Breakevens |
|---|---|---|---|
| bullish / high IV | bull put spread (credit) | 72% | [95.05] |
| bullish / low IV | bull call spread (debit) | 39% | [102.31] |
| neutral / high IV | iron condor (credit) | 66% | [92.78, 109.22] |
| neutral / low IV + catalyst | long strangle (debit) | 37% | [92.71, 108.29] |

## Analytics (`app/quant/analytics.py`)

Attached to every actionable plan as `TradePlan.analytics`:

- **Probability of profit** — risk-neutral, from the structure's breakeven(s)
  under lognormal dynamics (`prob_below`). Region-aware: above/below a single
  breakeven for directionals, *between* the wings for condors, *outside* for
  straddles/strangles.
- **Breakevens** — one or two, per strategy.
- **Net greeks** — position delta/gamma/theta($/day)/vega($ per IV point),
  aggregated across legs × contracts × 100.
- **Expected value** — POP-weighted, for defined-max-profit structures (rough).
- **is_credit** — whether the structure was opened for a net credit.

Greeks use the shared Black-Scholes model (`app/quant/pricing.py`).

## Backtesting credit structures

The paper engine and backtester price positions by **signed net** (debit > 0,
credit < 0), so P&L = `(current − entry) × contracts × 100` is correct for both.
Exits are credit-aware: `check_exit` measures P&L as a fraction of the capital
at stake via `abs(entry)`, so a +50% profit target means "capture 50% of the
debit paid" for a long and "capture 50% of the credit received" for a short.
Credit spreads and iron condors are therefore scored end-to-end — e.g. a bull
put spread wins on a rally and an iron condor wins when the underlying stays
range-bound. `run_backtest` no longer skips them.
