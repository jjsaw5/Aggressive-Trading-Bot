# Multi-agent options research system

A research funnel that uses LLM agents to **generate hypotheses**, providers to
**supply evidence**, application code to **validate and score**, and a human to
**decide**. It does not trade.

```
ORCHESTRATOR
   ↓
AGENT 1 — MARKET INTELLIGENCE     what could move the market, a sector, a name?
   ↓  MarketBrief
AGENT 2 — OPPORTUNITY GENERATOR   which of that is worth the cost of validating?
   ↓  ResearchCandidate[]
AGENT 3 — TRADE VALIDATOR         assume it is wrong; look for data that says so
   ↓  ValidationReport
DETERMINISTIC SCORING ENGINE      100 points, every one traced to a measurement
   ↓  CompositeScore
HARD REJECTION RULES              terminal; no score overrides them
   ↓
RANKED TRADE REPORT  →  HUMAN REVIEW  →  RESULT TRACKING
```

## Read these in order

| Document | What it covers |
|---|---|
| [SETUP.md](SETUP.md) | Install, run, credentials, the mock stack |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, data flow, the evidence ledger, the stage gate |
| [METHODOLOGY.md](METHODOLOGY.md) | Agent responsibilities, validation categories, contract selection |
| [SCORING.md](SCORING.md) | The 100-point rubric, abstention, hard rules, how to audit a score |
| [DATA_SOURCES.md](DATA_SOURCES.md) | FMP, Unusual Whales, Robinhood, news — what is wired and what needs keys |

## Run it

```bash
pip install -e ".[dev]"
alembic upgrade head
python run_market_scan.py
```

No credentials required. Providers default to the platform's mock stack and
agents to a credential-free deterministic runner; both are stamped on every
report and every stored row, so a mocked run can never be mistaken for a live one.

## The four claims this system makes about itself

1. **An agent cannot introduce a fact.** Python retrieves evidence and assigns
   ids first; agents may only cite those ids. A claim citing anything else is
   dropped and recorded. See `app/multiagent/agents/binding.py` and
   `tests/multiagent/test_evidence_binding.py`, which tests it with an agent
   built to lie.

2. **No LLM writes a number that reaches the score.** Every figure in the
   composite comes from a provider or from arithmetic over provider data. The
   agents contribute selection and interpretation.

3. **Absent is not zero.** A rule with no input abstains — its weight leaves the
   denominator — rather than scoring zero. Every score reports the coverage it
   was computed at.

4. **It places no orders.** There is no order-placement code path in this
   subsystem, no agent has an execution tool, and every persisted run records
   `execution_enabled = false`.

## What the score means

A deterministic rubric score, stamped **UNCALIBRATED**. No feature in this
repository has cleared out-of-sample validation
([docs/PRODUCT_STANCE.md](../PRODUCT_STANCE.md)), so a high score means "scores
well on the rubric", not "likely to make money". The label comes off when the
data earns it and not before.

## Relationship to the rest of the platform

This is an **additive subsystem**. It reuses the platform's provider
abstraction, domain models, Black-Scholes quant, risk policy and database, and
it touches none of the freeze-guarded scoring paths. `sd-scoring-2026.08-v4.1`
is untouched — see [ARCHITECTURE.md](ARCHITECTURE.md#freeze-isolation) and
`tests/multiagent/test_freeze_isolation.py`.
