# Short-Duration Trading Module — Phase 0 Discovery & Design

Status: **design, pre-implementation.** This document is the Phase 0 deliverable:
current-state assessment, reuse/gap analysis, provider matrix, data/API/UI/risk
design, and a phased backlog. No module code is written until the scope here is
approved. Live trading stays disabled throughout.

---

## 1. Objective (restated)

Add a dedicated **Short-Duration Trading** section for **0DTE** and **1–5DTE**
options, built on a *setup-first* principle: identify a valid market setup, then
decide whether a short-dated option is the right expression — never scan for
"cheap options expiring soon." News can create a candidate; news alone can never
approve a trade. Research / paper / human-approved-proposal modes only.

---

## 2. Current-state architecture (as verified in code)

Python 3.12 · FastAPI · SQLAlchemy **sync** on **Turso/libSQL** · vanilla-JS SPA.
Providers behind capability interfaces resolved from config by a registry. A
4-tier scan funnel (broad → watchlist → candidates → positions) driven by a
session-aware scheduler, an in-process event bus, an efficiency layer (TTL cache
+ token-bucket rate limiter + priority), a decision warehouse (snapshots →
outcomes → calibration), paper trading, and a Black-Scholes backtester.

Layering (from `docs/ARCHITECTURE.md`, reconciled with code): `domain` (pure
pydantic) → `providers` → `engine` → `risk` → `modes` → `services` → `api` /
`db`. Provider abstraction is strict: no engine/risk code imports a vendor.

### 2.1 Component inventory

| Component | Where | State |
|---|---|---|
| Market-data providers | `app/providers/fmp` (verified), `robinhood` (unverified), `mock` | Daily EOD + quotes only; **no intraday bars/VWAP** |
| Options-data (chain/greeks) | `unusual_whales`, `robinhood`, `mock` | Chain + IV; **greeks = BS-computed delta only**, gamma/theta/vega null on live feeds |
| Options-flow | `unusual_whales` (verified), `mock` | Sweep flag ✓, sentiment ✓; **no block flag, no venue** |
| News | — | **None.** `NEWS_PUBLISHED` event enum exists with no producer |
| Economic/macro calendar | `CalendarProvider` (FMP/mock) | **Earnings only**; `econ`/`fda` declared but never emitted |
| Broker | `robinhood` (read-only, unverified) | Equity account; positions read/import; **no live orders** |
| Signal engine | `app/engine/{flow,price_action,volatility,catalysts}.py` | Daily SMA/RSI + flow + IV; **no intraday/VWAP/ORB/relvol** |
| Scoring engine | `app/engine/scoring.py` | `composite_score` + confirmation multiplier; single general model |
| Risk engine | `app/risk/{policy,position_sizing,portfolio,trade_plan,exit_plan}.py` | Defined-risk sizing, exits — **DTE-keyed, not intraday** |
| Contract selection | `app/engine/contract_selection.py` + `liquidity.py` | `SelectionConfig(min_dte=20,max_dte=45)`, liquidity gates — **excludes 0DTE by default** |
| Scheduler | `app/scheduling/{clock,schedule,engine}.py` + `config/scheduling.yaml` | Session-aware; **10s tick floor, 15s fastest cadence** |
| Event bus | `app/events/{types,bus,detectors}.py` | Pub/sub built; **nothing subscribes — only logs** |
| DB models | `app/db/models.py` (7 tables) | JSON payload + indexed columns; **Alembic lags (3 tables only in `create_all`)** |
| API | `app/api/routes/*` (11 routers) | Consistent `APIRouter`+`response_model`+`run_in_threadpool` |
| Dashboard | `app/web/dashboard.html` + `routes/dashboard.py` | 6 tabs, vanilla JS; **HTML cached at import — edits need restart** |
| Paper trading | `app/services/paper_engine.py`, `position_import.py` | Signed-net slippage, MFE/MAE, `check_exit`; reused by backtest |
| Trade journal / warehouse | `app/services/outcomes_service.py`, `app/analytics/*` | snapshot → outcome → scorecard (Brier, POP/score buckets) |
| Backtest | `app/backtest/*` | Real underlying paths, **BS-repriced legs** (no option-quote history) |

### 2.2 Reuse / extend / keep-separate

**Reuse as-is (DTE-agnostic, already correct):**
- Pricing & greeks — `app/quant/pricing.py` (`black_scholes_greeks`, `prob_below`
  handle `t→0` correctly), `app/quant/analytics.py`.
- Risk sizing & admission — `app/risk/{policy,position_sizing,portfolio}.py`.
- Exit-plan builders — `app/risk/exit_plan.py` (parametrize `time_stop_dte→0/1`).
- Paper engine — `open/update/close/check_exit` + `SlippageModel` (signed-net safe).
- Execution guard / proposals / modes — `app/modes/*` (double-gated, reuse verbatim).
- Event bus, detectors, `bounded_gather`, metrics registry + `/metrics`.
- Market clock — sessions/holidays/early-close/DST complete.
- Warehouse loop — `snapshot → save → resolve → scorecard` (feed it a new `DecisionSource`).
- Persistence & async→sync conventions, router pattern, dashboard tab pattern.

**Extend:**
- **FMP provider** — add (after doc verification) intraday `1min`/`5min` bars,
  stock news, and economic calendar. These are real FMP endpoints not yet wired.
- **`OptionsChainProvider`** — a `get_option_chain_for_expirations` already exists;
  add greeks completion (BS gamma/theta/vega from per-contract IV) for short tenors.
- **`EventType`** — add short-duration events (see §7).
- **Alembic** — add a migration for new tables *and* backfill the 3 lagging ones.
- **Config** — new `SHORT_DURATION_*` settings block.

**Keep separate (new `app/shortduration/` package):**
- Intraday primitives (VWAP, opening range, relative volume, breadth proxy).
- Short-duration **market-regime** engine (intraday, breadth-aware).
- 0DTE & 1–5DTE **strategy modules**, **scoring models**, **candidate state
  machine**, **contract-selection rules**, **risk controls**, **position manager**.
- A **dedicated fast loop** — NOT a 5th tier. `Tier` is an `IntEnum` whose ordering
  the scheduler relies on; sub-15s cadence would head-of-line-block the shared
  10s tick behind the 500–1500-symbol Tier-1 sweep. The short-duration monitor
  runs its own loop, reusing `MarketClock`, `bounded_gather`, `Priority.POSITIONS`,
  and the event bus.

### 2.3 Technical debt / blockers surfaced

1. **No intraday data path.** All history is daily EOD. VWAP/ORB/relvol require
   intraday bars — the single largest build item. Mitigation: FMP intraday
   endpoints (verify) + compute primitives ourselves.
2. **No news pipeline** (dangling `NEWS_PUBLISHED` enum). Must build a
   `NewsProvider` capability + FMP implementation + latency instrumentation.
3. **No macro/economic calendar.** Must add an econ-calendar capability.
4. **No market breadth.** True advance/decline needs a feed we don't have. MVP
   uses a **transparent proxy**: % of our own liquid universe above VWAP / above
   opening range. Labeled as a proxy, never presented as true breadth.
5. **Greeks incomplete on live feeds** — only BS-delta today. Compute full BS
   greeks from per-contract IV for the short-duration chain view.
6. **Scheduler floor is 10s / 15s.** Sub-15s needs a separate loop (above).
7. **Event bus has no consumers** — the short-duration module will be the first.
8. **Dashboard HTML cached at import** — document the restart; consider a debug
   no-cache toggle.
9. **Alembic migrations lag `create_all`** — fix as part of new tables.
10. **No historical option-quote feed** — 0DTE/1–5DTE backtests will be
    **explicitly classified** (reconstructed / approximate / proxy / not-testable);
    never present approximate as precise.

---

## 3. Provider-capability matrix (short-duration needs)

| Need | Current source | Sufficient? | Action |
|---|---|---|---|
| Real-time quote (bid/ask/last) | FMP quote (delay caveated), RH | Partial | Reuse; label freshness via `Quote.as_of`/`delayed_minutes` |
| 1-min / 5-min OHLCV | — | **No** | Add FMP intraday (verify docs) |
| VWAP, opening range, prior-day/premarket H/L | — | **No** | Compute in `app/shortduration/levels.py` from intraday bars |
| Relative volume | — | **No** | Compute from intraday vs trailing avg |
| Option chain + full greeks | UW/RH chain; BS-delta | Partial | Reuse chain; complete greeks via BS from IV |
| Options flow (sweep/opening/sentiment) | UW (verified) | Yes | Reuse; add flow-decay layer; block flag = later |
| News (headline/source/timestamps) | — | **No** | Add `NewsProvider` + FMP news; instrument latency |
| Economic calendar (CPI/FOMC/NFP…) | — | **No** | Add econ-calendar capability (FMP, verify) |
| Earnings calendar | FMP/mock | Yes | Reuse |
| Market breadth / internals | — | **No** | Proxy from own universe (labeled); real feed later |
| Index options (SPX/XSP/NDX) | — | **No/unverified** | See §4 — deferred, config-gated |
| Broker positions (read) | RH (unverified) | Partial | Reuse read/import; no orders |

---

## 4. Instrument-scope decision (assumption, documented)

**MVP = equity/ETF options only:** SPY, QQQ, IWM, AAPL, MSFT, NVDA, AMD, META,
AMZN, GOOGL, TSLA, NFLX (the existing `DEFAULT_UNIVERSE`). Universe is
configurable.

**Index options (SPX / XSP / NDX) are deferred and config-gated**, because:
- The only broker integration is Robinhood (read-only, `verified=False`); index-
  option support/permissions are **unconfirmed** and must not be assumed.
- **Contract cost vs a ~$2,000 account:** one SPX contract carries ~$500k+
  notional and premiums frequently exceeding the account's per-trade risk cap
  (`min(equity×5%, $100)`); XSP (1/10 notional, cash-settled) is the only plausible
  index candidate for a small account.
- **Settlement/exercise differ** (cash-settled, European, AM/PM settlement) —
  needs explicit handling before enabling.

A per-instrument validation checklist (broker support, multiplier, liquidity,
spreads, permissions, settlement, assignment, practical cost) will gate any index
symbol before it enters the universe. Until validated: **excluded**.

### 4.1 XSP viability spike (Phase 1 finding)

Probed the broker directly (Robinhood connector, 2026-07-17):

| Check | Result |
|---|---|
| Broker support | **Yes** — XSP chain exists, `can_open_position: true` |
| Expirations | **Daily, including 0DTE** (e.g. 2026-07-20/21/22/23/24 …) |
| Multiplier | 100 |
| Settlement | Cash-settled, European (`underlying_instruments` empty); observed chain is PM-settled (`settle_on_open: false`) — **no early assignment / no share pin risk** |
| Current level | 745.77 → **~$74,600 notional per contract** |
| Extended hours | enabled |

**Affordability verdict for a ~$2,000 account (`min(equity×5%, $100)` = $100/trade cap):**
a naked near-ATM 0DTE XSP option (~$1.50–3.50/share → $150–350/contract) **exceeds
the per-trade risk cap** and would be rejected by sizing. A tight **defined-risk
debit spread** (1–2 points wide, net debit ~$0.40–0.90 → **$40–90 risk**) **fits**.

**Decision:** XSP is viable **spreads-only**, and stays **config-gated and OUT of
the Phase 1 default universe**. Enable it in Phase 4 (contract selection) behind
the validation checklist, forcing defined-risk structures. Open items before
enabling: confirm **data-feed coverage** for XSP (FMP intraday for the index; UW
options-flow/chain for XSP) — broker support is confirmed, market-data support is
not yet. SPX (~$746k notional) and NDX remain excluded for this account size.

---

## 5. Data model additions (`app/db/models.py` + Alembic migration)

New `*Row` tables (JSON `payload` + promoted indexed columns; add a real
migration and backfill the 3 lagging tables):

- `short_duration_candidates` — id, symbol(idx), dte_category(idx: 0dte|1-5dte),
  strategy, direction, detected_at(idx), market_regime, score, confidence,
  state(idx), entry_trigger, invalidation, targets(JSON), contract_reco(JSON),
  max_risk_usd, catalyst, data_quality_score, expires_at, payload.
- `candidate_state_transitions` — id, candidate_id(idx FK), from_state, to_state,
  at(idx), trigger, actor(system|user), reason, score_at, supporting(JSON).
- `intraday_levels` — symbol, session_date(idx), vwap, or_high, or_low, pm_high,
  pm_low, pd_high, pd_low, support(JSON), resistance(JSON), computed_at.
- `news_items` — id, symbol(idx), source_ts, provider_ts, received_ts, parsed_ts,
  candidate_ts, alert_ts, headline, summary, source, category, url, novelty,
  materiality, direction, relevance, dup_group_id(idx), confirmations(JSON),
  raw_ref, payload. (Latency = derived from the timestamp columns.)
- `flow_decay_state` — id, flow_event_ref, symbol(idx), original_score,
  decayed_score, age_seconds, confirmation_state, opposing_flow, candidate_id.
- `event_restrictions` — id, event_name, window_start, window_end,
  affected_symbols(JSON), affected_strategies(JSON), size_modifier,
  trading_allowed.
- Short-duration **performance** reuses the existing warehouse
  (`decision_snapshots`/`decision_outcomes`) via a new `DecisionSource.SHORT_DURATION`
  plus short-duration-specific fields in the snapshot payload (time-of-day,
  regime, catalyst type, DTE, delta band, news/flow-confirmed flags).

Raw provider payloads / immutable references retained for audit & reprocessing.

---

## 6. API additions (`/short-duration/*`, existing conventions)

Read: `GET /market-regime`, `/0dte/candidates`, `/1-5dte/candidates`,
`/candidates/{id}`, `/options/{symbol}`, `/flow/{symbol}`, `/news/{symbol}`,
`/events`, `/positions`, `/performance`, `/configuration`.
Action (gated, no live orders): `POST /scans/0dte`, `/scans/1-5dte`,
`/candidates/{id}/arm`, `/candidates/{id}/reject`, `/trade-proposals`,
`/paper-trades`, `/positions/{id}/close-recommendation`. No unrestricted
live-order endpoint is exposed — proposals still flow through the existing
`ExecutionGuard`.

---

## 7. Scheduling & event design

- **Dedicated short-duration loop** in `app/shortduration/loop.py`, gated by
  `SHORT_DURATION_ENABLED` (default false), reusing `MarketClock` for RTH gating.
  Cadences (config): open-position risk & selected-contract marks 5–15s; trade-
  ready candidates 15–30s; watchlist 30–60s; broad 0DTE 3–5m; 1–5DTE broad
  5–15m. Priority order: open positions → approved proposals → armed → ready →
  watchlist → broad, using the existing `Priority.POSITIONS` inversion so open
  risk always wins API contention.
- **New `EventType` members:** `SHORT_DURATION_CANDIDATE_DETECTED`,
  `CANDIDATE_ARMED`, `CANDIDATE_TRIGGERED`, `MATERIAL_NEWS_DETECTED`,
  `FLOW_ACCELERATED`, `PRICE_LEVEL_CROSSED`, `OPENING_RANGE_COMPLETED`,
  `VWAP_RECLAIMED`, `VWAP_LOST`, `BREADTH_CHANGED`, `ECONOMIC_EVENT_APPROACHING`,
  `ECONOMIC_EVENT_RELEASED`, `OPTION_LIQUIDITY_DEGRADED`, `QUOTE_STALE`,
  `POSITION_EXIT_RECOMMENDED`. Reuse existing `PRICE_CHANGED`, `FLOW_DETECTED`,
  `RISK_THRESHOLD_REACHED`, `MARKET_REGIME_CHANGED`, `POSITION_*`.
- The module is the **first real event-bus consumer**: handlers recompute on
  material change instead of waiting for the next poll.

---

## 8. Risk-control design (paper-first, all configurable)

0DTE baseline: 2–3% risk/trade (5% high-confidence ceiling), 1–2 concurrent, 5%
daily-loss stop, halt after 2 consecutive losses, defined-max-loss only, no
naked shorts, no averaging down, no entry on stale quotes / unverified broker
state / failed liquidity / restricted event window, default close before expiry.
1–5DTE baseline: 3–5% (7.5% ceiling), 15–20% aggregate short-duration exposure,
overnight-risk review, defined invalidation, min reward-to-risk. Time controls:
no first 5–15m entries, macro-blackout windows, 0DTE entry cutoff, 3:30/3:45
mandatory reviews, forced pre-expiry close. All layered on the existing
`RiskPolicy`; a `ShortDurationRiskPolicy` extends it with intraday/time rules.
These are **hard gates evaluated before scoring**, consistent with the platform's
"missing data disqualifies" principle.

---

## 9. Backtesting & paper approach

Backtests classify every result: **fully reconstructed / approximate / proxy /
not-testable**, and never present approximate as precise. With no historical
option-quote feed, 0DTE especially is **proxy-based** (BS reprice at realized
vol) until a per-contract feed is added — stated loudly. Paper trading reuses the
signed-net paper engine; a short-duration position monitor replicates the DTE +
**intraday time-stop** exits (time-stops are new — current exits are DTE-keyed).

---

## 10. Test plan & MVP acceptance

Per strategy and per risk rule: unit tests; fixtures for market-open, midday,
power-hour, news, and macro-event scenarios; scheduler/event/latency tests;
backtest classification tests; failure-simulation (stale quote, provider outage,
broker mismatch) tests. MVP acceptance = the 20 criteria in the brief, notably:
separate 0DTE/1–5DTE workspaces, regime + event-risk display, ranked & explained
candidates, live-ish option liquidity, flow/news/catalyst confirmation, separate
scoring models, contract/defined-risk recommendation with max-risk, rejection of
unsuitable trades, enforced time/event/liquidity/account-risk controls, stored
state transitions, paper trading, performance by strategy/regime/time/DTE, reuse
of existing components, tests, docs, no regressions, **live trading off**.

---

## 11. Phased backlog

- **Phase 0 — Discovery & design (this doc).** ✅ deliverable.
- **Phase 1 — Read-only module. ✅ DELIVERED.** New `app/shortduration/` package;
  extended FMP with intraday bars + news + econ calendar (grounded, `verified=False`
  pending live smoke test); new Benzinga news provider (premium, key-gated, FMP
  fallback); intraday primitives (VWAP/opening-range/relvol + breadth proxy);
  transparent short-duration regime engine + banner with allow-new/reduce-size
  gates; 6 new DB tables + idempotent Alembic 0002 (also backfilled the 3 lagging
  warehouse tables); `/short-duration/*` read routes + context-scan + manual state
  machine; new dashboard section (all 8 pages) rendering live data; 19 new tests.
  XSP viability spike done (§4.1). **No execution, no real detection** — boards
  populate from a context-only scan stub explicitly labeled non-actionable.
  Providers/module gated off by default; live trading unchanged.
- **Phase 2 — Strategy detection (subset). ✅ DELIVERED.** Four independent,
  configurable detectors in `app/shortduration/strategies/`: 0DTE opening-range
  breakout (confirmed close beyond the range, not a wick; VWAP-aligned; relvol
  gate), 0DTE VWAP continuation (above/below VWAP + sloped structure + pullback
  held VWAP + volume expansion); 1–5DTE trend continuation (reuses
  `analyze_price_action` for the daily trend + intraday alignment), 1–5DTE
  catalyst continuation (catalyst + **mandatory multi-session price/volume
  follow-through** — headline alone never triggers). `detection.py` builds a
  per-symbol `SetupContext`, runs the DTE's detectors, and persists real
  setup-first candidates (state DETECTED, provisional setup_score, entry trigger
  + invalidation + reasons) — replacing the Phase-1 context stub. Still no
  contract, no order. 12 new tests.
- **Phase 3 — Scoring & candidate state. ✅ DELIVERED.** Two SEPARATE weighted
  models in `app/shortduration/scoring/` (0DTE: intraday-structure 20 / market 15
  / relvol-momentum 15 / flow 15 / liquidity 15 / vol 10 / catalyst-news 5 / R:R
  5. 1–5DTE: daily-trend 20 / catalyst-news 15 / multi-session-flow 15 / market
  10 / vol 10 / liquidity 10 / technical-entry 10 / R:R 10), each factor carrying
  raw × weight = points + a plain-English reason. Named sub-scores (data-quality,
  liquidity, news/flow confidence, market alignment, execution, risk) + a
  data-quality-tempered overall confidence. News scoring (7-factor weighted +
  headline-similarity dedup + keyword direction classifier), flow decay
  (age-bucket weighting + opening/opposing/repeated-strike analysis — the
  provider label is an input, not truth), and a data-quality score that flags
  missing/stale inputs and lowers confidence (missing is never neutral). Full
  candidate state machine (legal-transition-enforced) wired into detection:
  DETECTED → EVALUATING → WATCHLIST/ARMED by score threshold, every transition
  audited. The dashboard renders the full breakdown + transition trail on click.
  14 new tests. Still no contract/order.
- **Phase 4 — Contract selection & risk. ✅ DELIVERED.** `app/shortduration/
  contracts.py` turns each scored setup into a sized, DEFINED-RISK expression,
  reusing the core selection + sizing + exit machinery with short-DTE configs
  (0DTE: 0–1 DTE, wider spread / cheaper mid tolerance; 1–5DTE: 1–5 DTE). Chains
  are fetched for the NEAR-TERM expirations (via `get_option_chain_for_expirations`,
  not the ~30-DTE default). Single-leg near-ATM first; if its debit exceeds the
  per-trade cap, a defined-risk debit vertical; if nothing liquid fits, the setup
  is REJECTED with the reason (illiquid vs unmanageable-risk) — never forced. The
  real reward-to-risk now feeds the risk/reward score factor. `app/shortduration/
  risk.py` adds a tighter per-DTE `short_duration_policy` (2–3% 0DTE / 3–5%
  1–5DTE, same $ cap, same-day time-stop for 0DTE) and HARD entry gates evaluated
  independent of score: market-closed / first-N-minutes / 0DTE cutoff, stale
  quote, daily-loss & consecutive-loss halts, concurrency, and regime blackout /
  reduce-size. Candidates carry the contract recommendation (legs, max loss/
  profit, breakevens, R:R), max risk, and the entry-gate verdict; the dashboard
  detail renders a Contract & risk card + gate status. Config adds the risk
  knobs. 10 new tests. Still no order — gates feed paper trading (Phase 5).
- **Phase 5 — Paper trading. ✅ DELIVERED.** `app/shortduration/paper.py` opens
  simulated positions from armed candidates (reusing the signed-net paper engine
  for fills/slippage/MFE/MAE), advancing the candidate ARMED → TRIGGERED → OPEN
  (the paper/research state path; live still needs PROPOSED → APPROVED + the
  ExecutionGuard). `monitor_short_duration_positions` marks each open position
  from the live chain and applies exits — profit/stop (paper engine) PLUS the new
  INTRADAY time-stops (0DTE force-close by 15:45 ET; DTE time-stop for 1–5DTE;
  expiry) — closing the paper trade and the candidate. New `short_duration_trades`
  table (+ migration 0003) tags each trade with strategy / regime / time-of-day /
  DTE / score band / news+flow confirmation for attribution. `daily_risk_state`
  computes today's realized P&L + trailing consecutive losses + open count and
  now feeds the Phase-4 entry gates (loop closed). `short_duration_performance`
  reports overall + sliced (by DTE, strategy, symbol, regime, time-of-day, score
  band, news-confirmed, flow-confirmed) win rate / avg win-loss / profit factor /
  expectancy / P&L. API: open-paper / positions / monitor / performance. Dashboard:
  Open Positions page + real Performance & Journal page + a paper button on the
  candidate detail. 7 new tests. Every fill simulated — no live order.
- **Phase 6 — Validation:** full test matrix, load/rate-limit/latency, backtest
  classification, failure simulations.
- **Phase 7 — Human-approved live proposals:** NOT enabled without explicit
  authorization through existing execution controls.

After every phase: run tests, show results, summarize files changed, note
decisions & unresolved risks, update this backlog, recommend the next phase.

---

## 12. Documented assumptions

1. Equity/ETF options only for MVP; index options deferred & config-gated (§4).
   XSP confirmed broker-viable, spreads-only (§4.1); still excluded from the
   Phase 1 default universe pending data-feed confirmation.
2. FMP supplies intraday bars, news, and economic calendar. Endpoints are wired,
   grounded on FMP's stable schema + the Benzinga v2 contract, but marked
   `verified=False` pending a live smoke test — the FMP doc leaf-pages return 403
   through the agent proxy (Cloudflare), so the field mapping is defensive and
   confirmed only against a live key. Same convention the codebase uses for the
   Robinhood provider.
3. Market breadth is a **proxy** from our own universe until a real internals
   feed is licensed; always labeled as such.
4. Short-duration runs as a **separate loop**, not a 5th scan tier.
5. Full greeks for the short-duration chain view are **BS-computed** from
   per-contract IV.
6. 0DTE/1–5DTE backtests are **proxy-classified** until a historical option-quote
   feed exists.
7. Live trading remains disabled; module operates research/paper/approval only.
