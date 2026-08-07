# Methodology — agent responsibilities and validation

The scoring rubric is in [SCORING.md](SCORING.md). This page covers what each
agent is for, what it is explicitly not for, and what Agent 3 measures.

## Division of labour

| | Agent 1 | Agent 2 | Agent 3 | Code |
|---|---|---|---|---|
| Identifies catalysts | ✅ | | | |
| Proposes tickers and direction | | ✅ | | |
| Chooses strategy type | | ✅ | | |
| Measures price, flow, IV, liquidity | | | | ✅ |
| Selects strikes and expirations | | | | ✅ |
| Interprets the measurements adversarially | | | ✅ | |
| Assigns the score | | | | ✅ |
| Applies hard rejections | | | | ✅ |
| Decides to trade | | | | 🧑 human |

The rightmost column is the point. Agents supply judgement about *what to look
at*; the numbers that decide anything come from data.

## Agent 1 — Market Intelligence

Answers one question: **what has a meaningful chance of moving the market, a
sector, or a name within the relevant horizon?**

Covers macro (CPI, PPI, PCE, GDP, employment, jobless claims, retail sales,
consumer confidence, ISM, yields, dollar), the Fed (FOMC, decisions, speakers,
minutes), market state (SPY, QQQ, IWM, VIX, breadth, regime), company catalysts
(earnings, guidance, revisions, ratings, price targets, M&A, product launches,
FDA, litigation, regulatory, filings, executives, investor days, conferences,
contracts) and sector catalysts.

It must classify each catalyst on five axes that are deliberately kept separate:

- **scope** — market / sector / company
- **expected direction** — bullish / bearish / volatile / neutral / **unknown**
- **importance** — critical / high / medium / low
- **horizon** — intraday / 1–3d / 1w / 2–4w / 1–3m
- **evidence quality** — `confirmed_fact` / `reported` / `interpretation` / `speculation`

A speculative reading of a rumour and a dated CPI print are both "catalysts";
the difference is the entire value of the classification. `volatile` and
`unknown` are legitimate and often correct answers for direction.

**Not responsible for**: proposing trades, touching options data, or producing a
number that feeds ranking. `importance_score` orders its own catalysts and is
never summed.

Index and VIX fields on the brief are written by code from provider data *after*
the agent returns, overwriting anything it said.

## Agent 2 — Opportunity Generator

Receives the brief and decides what is worth the cost of validating. It is not a
rubber stamp: disagreeing with Agent 1 — declining a catalyst it rated
important, reading a direction differently — is expected, and it says so in
`agent_reasoning_summary`.

Hard limits, enforced in code rather than requested of the agent:

- at most **10 candidates** per run
- only `long_call`, `long_put`, `bull_call_spread`, `bear_put_spread`
- one candidate per ticker per direction
- only tickers with retrieved data

**Prefer no trade.** An empty list is a valid and frequently correct answer.
"It has been going up" is not a reason; a reason names a mechanism and a rough
timeframe.

Every candidate must state an **invalidation** specific enough that a human
could check it tomorrow.

**Not responsible for**: any price, strike, expiration, IV, volume or Greek — it
has no live option data and is given none. No confidence score:
`preliminary_quality` is a three-value enum for ordering its own ideas and is
never summed.

## Agent 3 — Trade Validator

> Assume the candidate might be wrong, and look for data that confirms or
> rejects it.

Code measures six categories first; the agent then interprets them
adversarially. A validation that surfaces no disconfirming finding should be
rare and must say explicitly why nothing contradicts the idea.

### 1. Price / technical structure

An extensible indicator registry (`app/multiagent/analysis/technical.py`).
Adding an indicator is a decorator, not a model change:

```python
@register("my_indicator", requires_bars=30)
def _my_indicator(ctx: IndicatorContext) -> list[Measurement]:
    ...
```

Shipped: 20-bar trend, distance from the 20/50 SMA, relative volume, momentum
return and agreement ratio, ATR (absolute and % of price), higher-highs /
lower-lows structure, distance to swing support and resistance in ATR,
extension from the mean in ATR, opening gap, 20-bar rolling VWAP.

`requires_bars` is enforced by the runner, so an indicator never has to defend
itself against a short history: a 14-period ATR from 9 bars is not an ATR, and
reporting one would be a fabricated number wearing a real name. The rolling VWAP
is named `rolling_vwap`, not `vwap`, because true VWAP is session-anchored and
intraday — a different statistic.

### 2. Market alignment

SPY and QQQ bias from trailing returns, sector bias via an ETF proxy
(`SECTOR_PROXIES`), and relative strength oriented to the thesis direction — so
a bearish name underperforming a falling tape reads as strength *for the thesis*
rather than weakness. Alignment is tri-state; see [SCORING.md](SCORING.md).

### 3. Catalyst validation

Does it exist (do the cited refs resolve)? How old is the newest supporting
item? Is it corroborated? Has the underlying already moved with the thesis? Does
the event land inside the hold? What else is scheduled inside the hold?

The priced-in check compares price at publication to price now, and **abstains
rather than substituting a later bar** when the history does not reach back that
far — a close from three weeks after the catalyst answers a different question.

### 4. Options flow

Covered in [SCORING.md](SCORING.md#how-options-flow-is-scored-and-why-lightly).
The short version: premium not contract count, direction is a conclusion not an
input, and missing side/OI data is missing rather than negative.

### 5. Option contract quality

Expiration, strikes, bid, ask, spread as a fraction of **mid** (dividing by the
ask flatters a wide market), volume, open interest, delta, gamma, theta, vega,
IV, IV rank, term structure. Greeks are labelled `PROVIDER` or `MODELED`;
strike-invariant provider Greeks are detected and recomputed.

### 6. Risk / reward

Max loss, max profit, breakeven, breakeven move as a share of the IV-implied
move, reward-to-risk, distance to invalidation, theta burden over the hold, IV
crush exposure (a named 10-point scenario, not a forecast), event risk.

For an unbounded long option, reward-to-risk at expiry is undefined and reports
`None` rather than an invented cap; a target-based figure is computed to the
IV-implied move, which is market-derived rather than a made-up target.

**Not responsible for**: producing any number, selecting the contract, assigning
the score, or overriding a hard rejection.

## Contract selection

Runs after the thesis validates, against a freshly-retrieved chain, and never
before the options market opens.

**Do not select a contract merely because it is cheap.** The ranking blends
delta fit, spread tightness, liquidity and (for verticals) payoff ratio.
Premium is not a term — it enters only as the risk-budget constraint.

| | Long call / put | Bull call / bear put spread |
|---|---|---|
| DTE band | 7–45 | 7–45 |
| Long delta | 0.35–0.65 | 0.40–0.70 |
| Short delta | — | 0.15–0.40 |
| Widths tried | — | configured dollars **plus** 1/2/4× the chain's own strike increment |

Absolute dollar widths alone break at both ends of the price range — $2.50 is a
12.5% spread on a $20 stock (short leg below the delta band, so no spread is
ever built) and barely one strike on a $900 one. Strike-relative widths adapt to
the grid the underlying actually trades on.

**Sizeability dominates the ranking.** A structure that cannot be sized inside
the $100 per-trade cap is not a cheaper trade, it is not a trade; ranking it
first would hand every candidate to the rules engine to reject.

**Spread fallback.** A single long option on a $130 underlying routinely costs
more than the risk cap. Rejecting the whole candidate for that would discard a
validated thesis over an expression choice the agent was told not to make — so
the defined-risk vertical is offered alongside, with the reason recorded. This
never loosens the cap (the fallback is a cheaper structure, not a bigger budget)
and never introduces a strategy outside the allow-list.

## Risk limits

From [RISK_POLICY.md](../RISK_POLICY.md), enforced as hard rules:

**$100 max defined risk per trade · $300 aggregate heat · 4 concurrent
positions · 20 contracts per trade.**

Position size is the largest contract count whose defined risk fits the budget,
floored, never rounded up.

## Human decision tracking

`approved` · `rejected` · `watched` · `entered` · `skipped`, recorded via
`POST /multiagent/decisions`. If entered, `POST /multiagent/executions` records
what the human actually did — the system places no orders — and
`POST /multiagent/results` records the outcome.

MFE and MAE are stored as `max_favorable_excursion_bound` and
`max_adverse_excursion_bound`. They come from bar extremes that have no ordering
within the bar; they are bounds, not achieved prices, and the field names carry
the caveat so it cannot be lost on the way to a spreadsheet.

## Future performance engine

Not implemented; the schema is shaped for it. The questions it must answer —
do 80+ trades outperform 70–79, which components predict, which catalysts work,
which strategy suits which volatility regime, does flow help, which DTE and
delta ranges perform, **how often did rejected trades actually work** — all
require the decision-time state, which is why every run stores its measurements,
its per-rule audit trail, and its rejections.

**No AI self-modification of scoring rules.** Analysis may recommend; a human
edits `config/methodology.yaml` and bumps the version.
