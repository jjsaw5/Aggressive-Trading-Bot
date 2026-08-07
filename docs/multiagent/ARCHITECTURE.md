# Architecture

## The organising principle

> AI generates hypotheses. APIs provide evidence. Code validates and scores.
> The human decides.

That only holds if "evidence" is something the code owns rather than something
an agent asserts. Everything below follows from making that literally true.

## Layers

| Layer | Package | Depends on |
|---|---|---|
| Config | `app/multiagent/config.py`, `runtime.py` | nothing |
| Models | `app/multiagent/models/` | `app.domain` (reused enums and market types) |
| Evidence | `app/multiagent/evidence/` | providers, models |
| LLM | `app/multiagent/llm/` | models |
| Analysis | `app/multiagent/analysis/` | models, `app.quant` |
| Selection | `app/multiagent/selection/` | models, `app.quant` |
| Agents | `app/multiagent/agents/` | llm, evidence, analysis, selection |
| Scoring | `app/multiagent/scoring/` | models, config |
| Rules | `app/multiagent/rules/` | models, config |
| Orchestrator | `app/multiagent/orchestrator.py` | all of the above |
| Reports | `app/multiagent/reports/` | models |
| Persistence | `app/multiagent/db/` | models, `app.db` |
| API / CLI | `app/api/routes/multiagent.py`, `run_market_scan.py` | orchestrator |

Dependencies run one way. `scoring/` cannot import `llm/`, which is what makes
"no LLM output reaches the score" a structural property rather than a habit.

## Data flow, one run

```
resolve_stage(requested, now)                  market clock; may downgrade to premarket
   │
   ▼
EvidenceCollector.collect_market_evidence()    quotes · history · news · econ · earnings
   │  every artifact gets a stable id
   ▼
EvidenceLedger  ─────────────────────────────► the ONLY thing agents may cite
   │
   ▼
Agent 1  → bind_claims() → MarketBrief         measured index fields overwritten by code
   │
   ▼
Agent 2  → bind_claims()                       + ticker whitelist, strategy allow-list,
   │        restrict_to_known_symbols()          candidate cap, reference price from quote
   ▼
ResearchCandidate[]
   │
   ▼  per candidate, bounded concurrency
fetch_symbol_data()                            fresh quote · history · chain · IV · flow
   │
   ▼
build_measured_report()                        technical · alignment · catalyst · flow
   │                                           · contract quality · risk/reward
   ├─ propose_structures()                     long option + defined-risk spread fallback
   │
   ▼
Agent 3 → interpretation only                  verdicts, findings; writes no number
   │
   ▼
score_candidate()                              8 categories → CompositeScore + audit trail
   │
   ▼
evaluate_hard_rules()                          terminal; never sees the score
   │
   ▼
classify() → rank() → RankedReport             ranked + rejected, both persisted
```

## The evidence ledger

The anti-hallucination control, and the reason the ordering above is fixed.

`EvidenceCollector` calls providers **first** and mints a deterministic id for
every artifact (`news-9dd8429583`, `econ-0b345a127d`, …). Each `EvidenceItem`
carries its source, URL, `published_at` and `retrieved_at` — both timestamps,
always, because "when it happened" and "when we saw it" are different questions
and an undated item is not a fresh one.

Agents are shown the ledger and told they may cite only those ids. On the way
back, `bind_claims()`:

| Case | Outcome |
|---|---|
| all refs resolve | claim kept, bound to real artifacts |
| some refs resolve | claim kept, unknown refs **stripped** |
| no refs resolve | claim **dropped**, `UNREFERENCED_AGENT_CLAIM` recorded |
| refs required and absent | claim **dropped** |

Every drop lands in three places: `RunDiagnostics.dropped_agent_claims`,
`AgentRunRecord.dropped_claims`, and the `ma_data_quality_flags` table. An agent
that invents a headline therefore produces a *visible absence*, not a silent
insertion.

Two further restrictions on Agent 2: candidates on tickers with no retrieved
data are dropped (they could not be validated), and the underlying reference
price is written from the real quote after parsing, overwriting whatever the
agent said.

## Agent execution

`.claude/agents/*.md` are the **single source of truth** for agent roles. The
same file is:

* a Claude Code subagent definition (frontmatter `name`, `description`, `tools`), and
* the system prompt for the Python runtime (the markdown body).

The alternative — a prompt string in Python plus a markdown file describing the
same role — guarantees drift, and the drift is invisible until an agent behaves
differently depending on how it was invoked.

Two runners implement `AgentRunner`:

| Runner | `runner_id` | Needs credentials | Notes |
|---|---|---|---|
| `DeterministicAgentRunner` | `deterministic` | no | Heuristic stand-in for judgement. Cites real ledger ids; never invents data. Ships as the default. |
| `AnthropicAgentRunner` | `anthropic:<model>` | `ANTHROPIC_API_KEY` | Schema-forced tool output, not "please reply with JSON". |

An unknown runner name **raises**; asking for `anthropic` without a key
**raises**. Neither falls back silently, because a corpus whose stated author is
wrong is worse than a run that did not happen. The runner id is stamped on every
report, every `AgentRunRecord` and the `ma_runs` row.

Both runners' outputs go through identical Pydantic parsing and evidence
binding, so the validation path is exercised by every test rather than only when
a key is present.

## Premarket vs market-open

Option quotes before the options market opens are stale, one-sided or absent. A
structure selected against them is priced against a fiction — and in a report it
looks exactly like a real one.

| Stage | Agents 1–2 | Chain retrieved | Contracts selected |
|---|---|---|---|
| `PREMARKET` | yes | **no** | **no** |
| `MARKET_OPEN` | yes | yes | yes |
| `FULL` | yes | yes | yes |

`resolve_stage()` reads the platform's existing `MarketClock`, so this subsystem
and the rest of the platform cannot disagree about whether the market is open. A
`FULL` run requested outside market hours is **downgraded to premarket**, not
failed and not run anyway — the research half stays available, and the report
states in its header that contracts were not finalised and why.

## Measurement and absence

Measurements are never bare floats. `Measurement` is either present (value,
provenance, source, timestamp) or absent (with an `AbsenceReason`:
`NA_not_implemented`, `NA_no_data`, `NA_unresolved`, `NA_provider_error`,
`NA_stale`). `Measurement.value` is typed `float | None`, so code that reaches
for the number without checking raises on arithmetic instead of silently
computing with a zero.

`Provenance` records where a number came from: `PROVIDER`, `DERIVED`, `MODELED`,
`AGENT`. Nothing marked `AGENT` is an input to scoring arithmetic. Anything
`MODELED` — Black-Scholes Greeks, probability of profit — is labelled as such
everywhere it surfaces.

One consequence worth naming: the selector detects **strike-invariant Greeks**.
A provider reporting one constant gamma/theta/vega for every strike is filling
the field, not measuring it, and taking it at face value makes a vertical's net
theta come out as exactly zero — which silently passes the theta-burden rule and
makes the excessive-theta hard rule unfireable. Those Greeks are recomputed from
Black-Scholes and relabelled `MODELED`.

## Observability

Every run persists:

* `ma_runs` — stage, status, methodology version, runner, `execution_enabled`
* `ma_agent_runs` — prompt and response excerpts (**redacted**), timings,
  structured output, dropped claims, validation warnings, token counts
* `ma_data_provider_requests` — provider, capability, symbol, duration, outcome.
  Deliberately no URL and no headers: those carry credentials.
* `ma_data_quality_flags` — gaps, staleness, provider disagreement, dropped claims
* `ma_technical_snapshots`, `ma_options_flow_snapshots`,
  `ma_option_contract_snapshots` — the market state a decision was made against
* `ma_score_components` — one row per category, with the per-rule audit trail
* `ma_trade_recommendations` — ranked **and** rejected

Rejections are stored deliberately. The future performance engine's most
valuable question is "how often did rejected trades actually work?", and it can
only be answered if the rejection was recorded at the time with the data that
produced it.

## Freeze isolation

`CLAUDE.md` §2 freezes `sd-scoring-2026.08-v4.1`, and FINDING_01 established
that the freeze is about **behaviour, not about which files you edited**. This
subsystem is additive, and `tests/multiagent/test_freeze_isolation.py` makes
that mechanical:

* it does not import `app.shortduration.scoring`, `.strategies`, `.contracts`,
  `app.engine.contract_selection` or `app.engine.iv_context`;
* the frozen model does not import it;
* no file it owns matches CI's `GUARDED_RE`;
* the richer news corpus lives in `app/multiagent/providers/mock_research.py`
  rather than in the freeze-guarded `app/providers/mock/provider.py`;
* it constructs no `IVContext`, so it cannot change which scored fields are
  populated — FINDING_01's actual mechanism;
* its migration is `CREATE TABLE` only, all tables prefixed `ma_`;
* `settings.scoring_model_version` is unchanged.

The two version strings are deliberately distinct — `ma-methodology-2026.08-v1`
and `sd-scoring-2026.08-v4.1` — and both are stored on every run, so a row can
never be ambiguous about which model produced it.

## Future: the risk reviewer

`.claude/agents/risk-reviewer.md` exists and is **not wired**. Its limits are
settled before anyone enables it: it may reduce a rank and attach warnings, it
may never raise a score or clear a hard rejection, and any downgrade must name
the measurement that justifies it and is recorded as a separate attributable
adjustment. Allowing an agent to *add* points would make the composite
unreproducible and quietly reintroduce the "LLM assigns the confidence number"
behaviour the architecture exists to prevent.
