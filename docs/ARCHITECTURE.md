# Architecture

## Principles

1. **Provider abstraction everywhere.** No engine/risk/service code imports a
   concrete vendor. Capabilities (`MarketData`, `Fundamentals`, `OptionsChain`,
   `OptionsFlow`, `Calendar`, `Brokerage`) are abstract interfaces; the registry
   resolves them from config. Swapping FMP → Polygon, or live → mock, is a
   one-line config change.
2. **Verify before integrating.** Every provider carries `ProviderMeta`
   (auth/delay/rate-limit/licensing/docs/`verified`). Endpoints are grounded in
   official docs; response fields are parsed defensively. Nothing is invented.
3. **Capital preservation first.** Missing data disqualifies rather than
   assumes. Hard liquidity gates run before scoring. Every actionable candidate
   has bounded, sized, defined risk that fits the policy.
4. **Traceability.** Rejected candidates keep their reasons. Structured logs
   carry `scan_id`, symbol, and risk numbers. The domain object is persisted as
   a JSON payload for replay alongside indexed ranking columns.
5. **No silent execution.** Live orders pass a single guarded chokepoint that is
   off by default and requires explicit human approval.

## Data flow (one scan)

```
UniverseConfig
   │  normalized symbols
   ▼
for each symbol ──────────────────────────────────────────────┐
   │ MarketData.get_quote / get_price_history                  │
   │ Fundamentals.get_fundamentals                             │
   │ OptionsChain.get_iv_context / get_option_chain            │
   │ OptionsFlow.get_flow_alerts                               │
   │ Calendar.get_catalysts                                    │
   ▼                                                           │
gate_underlying()  ── penny/low-vol/low-float → REJECTED       │
   ▼                                                           │
analyzers → SignalScore[]                                      │
   flow · price_action · volatility(direction-aware) · catalyst│
   ▼                                                           │
resolve_direction() + composite_score()                        │
   (confirmation multiplier: flow×price agreement)             │
   ▼                                                           │
select_long_contract()  ──► if unsizeable ──► select_vertical_spread()
   ▼                                                           │
build_*_plan()  → sized, defined-risk TradePlan                │
   ▼                                                           │
evaluate_admission()  ── portfolio heat / max positions → REJECTED
   ▼                                                           │
TradeCandidate (RANKED | REJECTED, full thesis + reasons) ─────┘
   ▼
sort by composite_score  →  store  →  API / CLI / scheduler
```

## Layers

| Layer | Package | Depends on |
|---|---|---|
| Domain | `app/domain` | nothing (pure Pydantic) |
| Providers | `app/providers` | domain |
| Engine | `app/engine` | domain, providers (interfaces) |
| Risk | `app/risk` | domain, config, engine (selection DTO) |
| Modes | `app/modes` | domain, config, risk |
| Services | `app/services` | engine, risk, providers registry |
| Short-duration | `app/shortduration` | domain, providers, risk, quant |
| API | `app/api` | services, modes |
| Persistence | `app/db`, `alembic` | config, domain |

The **Short-Duration Trading** module (0DTE / 1–5DTE) is a separate subsystem
that reuses the pricing, risk-sizing, exit-plan, paper-engine, event-bus, market-
clock, and warehouse machinery while adding its own intraday primitives (VWAP,
opening range, relative volume, breadth proxy), a transparent intraday regime
engine, new provider capabilities (`IntradayProvider`, `NewsProvider`,
`EconomicCalendarProvider`), and its own persistence/API/dashboard surface. It is
setup-first — confirm a market setup, then decide whether a short-dated option is
the right expression — and runs research/paper/approval only. See
`docs/SHORT_DURATION.md` for the full design and phase status.

## Modes & the execution guard

`modes/execution_guard.py` is the only path to a live order. It denies unless:
1. the proposal is **APPROVED** (never implied), **and**
2. `TRADING_MODE=automation` **and** `AUTOMATION_ENABLED=true` (double gate), **and**
3. the proposal's defined risk still fits the policy at execution time.

Every attempt is logged. The current build places **no** broker orders; the
`/proposals/{id}/execute` endpoint exposes the guard decision so the safety gate
is observable and testable.

## Persistence

`db/models.py` promotes ranking/filter fields (symbol, status, score, risk) to
indexed columns and stores the full domain object as a JSON `payload` for
replay. The API currently uses an in-memory `services/store.py` so it runs with
zero infrastructure; swapping to a DB repository is localized because routes
depend only on the store's functions.

## Testing

`pytest` covers risk sizing (caps never breached), portfolio admission, scoring
+ confirmation, liquidity gates, the paper engine (MFE/MAE, exits), the
execution guard (denials), and a full mock-backed scan pipeline + API smoke
test. The mock provider is deterministic (seeded per symbol) so tests and
backtests are reproducible.
