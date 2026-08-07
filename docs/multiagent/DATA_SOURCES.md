# Data sources

This subsystem adds **no new vendor integrations**. The platform already has a
capability abstraction (`app/providers/base.py`) with FMP, Unusual Whales,
Robinhood, Benzinga and a mock stack behind it; duplicating that would be the
worst possible outcome. The multi-agent pipeline resolves providers through the
existing registry.

Vendor detail lives in [`docs/providers/`](../providers/):
[FINANCIAL_MODELING_PREP.md](../providers/FINANCIAL_MODELING_PREP.md) ·
[UNUSUAL_WHALES.md](../providers/UNUSUAL_WHALES.md) ·
[ROBINHOOD.md](../providers/ROBINHOOD.md).

## What the pipeline consumes

| Capability | Used for | Interface |
|---|---|---|
| `MarketDataProvider` | quotes, price history for indices, sector proxies and candidates | `get_quote`, `get_price_history` |
| `FundamentalsProvider` | sector (→ sector proxy), company name, float | `get_fundamentals` |
| `OptionsChainProvider` | the chain contracts are selected from, IV context | `get_option_chain`, `get_iv_context` |
| `OptionsFlowProvider` | flow prints for the flow snapshot | `get_flow_alerts` |
| `CalendarProvider` | earnings dates, dated company catalysts | `get_earnings`, `get_catalysts` |
| `NewsProvider` | headlines with source, URL and timestamps | `get_news` |
| `EconomicCalendarProvider` | CPI, FOMC, claims, ISM — scheduled macro | `get_economic_events` |

Swapping a vendor is a `PROVIDER_*` environment change. Nothing in
`app/multiagent/` imports a concrete vendor client.

## Status by vendor

| Vendor | Status | Needs |
|---|---|---|
| **Mock** (platform) | ✅ working, default | nothing |
| **Mock research** (this subsystem) | ✅ working, default for news/econ | nothing |
| **Financial Modeling Prep** | 🔑 client exists; wire via `PROVIDER_*=fmp` | `FMP_API_KEY` |
| **Unusual Whales** | 🔑 client exists; flow, IV history, chain | `UW_API_KEY` |
| **Robinhood** | 🔑 client exists; **read-only** account/chain | `RH_*` + `pip install -e ".[robinhood]"` |
| **Benzinga** | 🔑 client exists (news) | `BENZINGA_API_KEY` |
| **Anthropic** | 🔑 optional; enables the LLM runner | `ANTHROPIC_API_KEY` + `pip install -e ".[llm]"` |
| **Web search / general news** | ⛔ not wired | see below |

Everything works today without a single credential. The mock stack is
deterministic, so scans are reproducible and tests are hermetic.

## The research mock

`app/multiagent/providers/mock_research.py` supplies a richer news and economic
corpus than the platform mock, which emits one identical headline per symbol —
correct for the latency tests it was written for, but it exercises exactly one
branch of catalyst classification. The research corpus varies on the axes the
pipeline reasons over: catalyst type, sentiment, scope, evidence quality and age.

It lives here rather than in `app/providers/mock/provider.py` because that file
is **freeze-guarded** (`GUARDED_RE` in `.github/workflows/ci.yml`); extending it
would implicate the capture window for a subsystem that has nothing to do with
it.

Everything it emits is unmistakably synthetic: sources are `mock-*`, URLs point
at `example.test`, and every summary says so — which matters, because the whole
architecture rests on being able to tell retrieved fact from generated text.

## Robinhood is read-only, structurally

The MVP uses Robinhood for option chains, quotes, spreads and account state.
**No order-placement path exists in this subsystem.** No agent is given an
execution tool; no route under `/multiagent` can submit an order
(`tests/multiagent/test_api.py` asserts it against the route table); every
persisted run records `execution_enabled = false`.

The platform's live-order chokepoint is `modes/execution_guard.py`, which is off
by default and behind a double gate. This subsystem does not reach it and does
not import it.

## Web and general news — not wired

The architecture supports it: `NewsProvider` is a capability, and adding a
retriever means implementing one interface. It is not wired for Milestone 1
because doing it properly means source-reputability policy, deduplication across
outlets, and paywall handling — none of which should be improvised.

The evidence model is already shaped for it. Every `EvidenceItem` carries
`source`, `url`, `headline`, `published_at`, `retrieved_at`, optional `symbol`,
a catalyst classification and an `EvidenceQuality`. Social-media content, when it
arrives, must be classified `SPECULATION` — the agent definitions say so
explicitly, and unverified chatter is not market information.

## Provider failure policy

**A miss is recorded, never filled.** Every call is timed and bounded
individually; an error becomes a `ProviderRequestRecord` with `ok=False`, an
entry in `ledger.provider_errors`, and a line in the report's diagnostics. The
run continues with less evidence and the affected scoring rules abstain.

The distinction is preserved end to end: *"no news exists for NVDA"* and *"the
news provider returned 503"* lead to different conclusions, and the report says
which happened.

## Cross-provider agreement

The underlying price is checked against a second, independent source — the
option chain carries its own underlying mark. Where they differ:

- within 1% → the `data.providers_agree` rule awards its points
- beyond 2% → `PROVIDER_DISAGREEMENT` hard rejection
- only one source answered → the rule **abstains**; reporting 0% disagreement
  would claim the sources agreed when only one of them spoke

## Credentials

Secrets live only in `.env` (gitignored) or the deployment's secret manager and
reach code through the environment. Never a commit, a log line, a prompt, a
report, a database row or a chat transcript.

Agent prompts contain retrieved *data*, never credentials — providers are called
by Python before agents run. As a backstop, `AgentRunRecord.record_prompt` and
`record_response` redact anything matching a credential pattern before storage,
and a test asserts it.

CI runs gitleaks (`.gitleaks.toml`); that job is a backstop, not the first line.
Secret-scan every staged diff before committing. A key written down anywhere but
a secret store is already compromised — rotate it.
