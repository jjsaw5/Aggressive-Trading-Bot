# Short-Duration Engine — Improvement Phase (v2) · Phase 0 Assessment

**Objective:** strengthen data quality, execution realism, and risk management for the 0DTE and
1–5DTE workflows before deeper paper forward-testing. **Not** adding strategies. **Not** rebuilding.

**Non-negotiable:** the double execution gate stays intact, live execution stays disabled by default,
every trade plan stays defined-risk, every rejection stays explainable.

This document is the required Phase-0 deliverable: current-state analysis, file-level change map,
schema plan, backward-compatibility risks, acceptance criteria, test plan, and the phased backlog.
No production code changes until this plan is agreed.

---

## A. Current-state assessment (by subsystem)

The codebase is healthy and the abstractions are the right ones to build on. Key architectural facts
that shape this plan:

- **Providers** are capability interfaces in `app/providers/base.py`, resolved by `registry` from
  `PROVIDER_*` env vars, cached as `@lru_cache` singletons. Adding a capability = new interface +
  registry accessor + concrete/mock impl. **This is how we add MarketInternals and AccountState.**
- **Persistence** uses a *promoted-columns + JSON `payload`* pattern (e.g. `ShortDurationCandidateRow`,
  `ShortDurationTradeRow` in `app/db/models.py`). New fields that are **not** queried can live in
  `payload` with **zero migration**; only fields we filter/sort on need real columns + Alembic.
- **Migrations** are add-only and inspector-guarded (`alembic/versions/0001–0003`, plus `create_all`
  on startup). We continue that pattern — no destructive changes.
- **Config** is pydantic `BaseSettings` (`app/config.py`), env = field-name uppercased. New knobs are
  additive with safe defaults.
- **Scoring weights are currently hard-coded literals** in `app/shortduration/scoring/engine.py`
  (lines 66–86). They must move to versioned config.

| Concern | Current implementation (file · fact) | Gap vs. review |
|---|---|---|
| Relative volume | `app/shortduration/levels.py :: relative_volume()` — flat proration: `expected = avg_daily_vol × minutes_elapsed/390` | Needs historical time-of-day cumulative-volume profile, median-based |
| Opening-range breakout | `app/shortduration/strategies/orb.py` — fixed `min_break_pct=0.0005` | Needs adaptive buffer (max of bps / ATR / OR-width), anti-chase, confirmation modes |
| VWAP continuation | `app/shortduration/strategies/vwap_continuation.py` — hard gate: *no* bar may close wrong side of VWAP | Needs continuation-quality model allowing controlled reclaim |
| Regime / breadth | `regime.py`, `breadth.py`, `service.py` — % of universe above VWAP treated as "breadth", gates trend | Rename → `watchlist_participation`; add MarketInternals abstraction; cap confidence on proxy-only |
| Quote freshness | `scoring/data_quality.py :: quote_is_stale()` + `_QUOTE_STALE_SECONDS=120` — single global threshold | Needs per-use-case/state/track policy; block armed 0DTE on stale |
| Contract selection | `app/shortduration/contracts.py` + `app/engine/contract_selection.py` — delta band + moneyness fallback | Reused as-is; feeds new exit planner + account-aware sizing |
| Risk planning | `app/shortduration/risk.py :: short_duration_policy()`, `app/risk/policy.py :: RiskPolicy` | Sizing reads `settings.account_equity_usd` constant → AccountState abstraction |
| Exit planning | `app/risk/exit_plan.py` (`ExitPlan`/`ExitLevel` in `app/domain/trades.py`), `paper.py :: _intraday_exit` | Fixed 50/50 + time-stop only; needs layered structure-aware plan |
| Account equity | `settings.account_equity_usd = 2000.0` (constant) | New `AccountStateProvider`, paper/live/fallback, verified flag |
| Paper trading | `app/shortduration/paper.py`, `app/services/paper_engine.py`, `ShortDurationTradeRow` | One book; needs Signal-Validation vs Account-Executable split |
| Performance | `paper.py :: short_duration_performance()` | Needs per-book rollups, no cross-contamination |
| News scoring | `app/shortduration/scoring/news.py` — 7-factor keyword model | Keep; add structured event-classification layer alongside |
| 0DTE weights | `scoring/engine.py` lines 66–73 (hard-coded) | Rebalance + move to versioned config |
| Scheduler | `app/shortduration/loop.py` — RTH-gated cadences | Unaffected; may add profile-warm job |
| Safety | `app/modes/execution_guard.py` double-gate | **Preserve exactly** |

## B. Reusable components (leverage, don't rebuild)

- `MarketClock` / session calendar (`app/scheduling/clock.py`) → holidays, early-close for the volume profile.
- Provider registry + `@lru_cache` pooled clients → MarketInternals & AccountState slot in cleanly.
- `RiskPolicy` + `short_duration_policy()` → extend, don't replace, for account-aware sizing.
- `ExitPlan`/`ExitLevel` domain models → extend with named layers rather than a new type.
- JSON-`payload` row pattern → most new candidate/trade fields need no migration.
- `metrics` registry (`/metrics`) + dashboard `autogrid` cards + `stratLabel`/ranked board JS → observability & UI reuse.
- Mock provider (`app/providers/mock/provider.py`) → default impls for new capabilities keep tests hermetic.

## C. Schema-change plan (add-only, migration `0004`)

Promoted **columns** (queried/filtered) — new Alembic `0004`, inspector-guarded, `create_all`-compatible:

- `short_duration_trades.book` (String, default `signal_validation`) — Book A/B split (filtered).
- `short_duration_trades.executable_at_entry` (Bool, nullable) — analytics filter.
- `short_duration_candidates`: `scoring_model_version`, `risk_policy_version` (String, nullable) — filter/rollup.
- New table `intraday_volume_profiles` (symbol, session_date, payload of per-minute medians, sample_count, updated_at) — cache.
- New table `market_internals_snapshots` (captured_at, source, payload) — optional cache/audit.
- New table `account_state_snapshots` (captured_at, source, is_paper, payload) — audit.

Everything else (exit-plan layers, freshness results, structured news event, quote-age fields,
per-factor sub-scores) rides in existing `payload` JSON → **no migration**.

**Migration strategy:** add-only, nullable/defaulted, guarded by an inspector check (mirrors `0002/0003`);
existing rows keep working (old trades default to `book=signal_validation`, versions `NULL`).

## D. Backward-compatibility risks & mitigations

1. **Scoring weight change alters scores/states.** → Version the weights (`scoring_model_version`);
   old candidates keep their recorded version; new default `sd-0dte-v2`. Tests assert weights sum to 100.
2. **Relvol semantics change** could shift ORB/score behavior. → Profile is *additive*; flat model
   remains as an explicitly-labeled `estimated` fallback when history < min sessions. Missing relvol still
   cannot inflate a score (`_MISSING_RAW=0.25` unchanged).
3. **VWAP rule loosening** may admit more candidates. → New continuation-quality score gates via a
   configurable floor; default tuned to be no looser than today at the margin, with tests for choppy rejects.
4. **Freshness tightening** may reject candidates that pass today. → New thresholds apply to *armed/triggered*
   0DTE only; broad-screen stays 120s. Fully configurable.
5. **Account-state abstraction** must not accidentally enable live. → Fallback equity is always `is_paper`/
   `unverified`; live-executable requires a *verified broker* source; execution guard untouched.
6. **`market breadth` rename** touches regime + UI + API. → Keep a back-compat read; new field
   `watchlist_participation` added, old key retained one release as an alias, dashboard/API updated together.
7. **Two paper books** must never merge into one headline number. → Enforced at the reporting layer +
   a test asserting no cross-book aggregation.

## E. Acceptance criteria (maps to review §17)

Tracked per phase; overall "done" = review's 18 criteria, condensed: profile-based relvol; participation
un-labeled-as-breadth; MarketInternals abstraction; stricter armed-0DTE freshness; layered exit plans;
adaptive ORB; controlled-reclaim VWAP with quality scores; account-state sizing; unverified-fallback
labeling; two paper books; configurable+versioned 0DTE weights; structured mixed-outcome news; safety gates
intact; existing functionality green; migrations included; tests green; docs + METHODOLOGY.md updated.

## F. Test plan (per phase, all hermetic via mock providers)

Each phase ships unit tests enumerated in the review (e.g. volume profile: open/midday/power-hour/early-close/
missing-bars/outlier/insufficient-history/zero-volume). Plus: migration up/idempotent test; regime proxy-vs-real
tests; freshness state-transition tests; exit-layer trigger tests; account-state sizing tests; book-separation
tests; news mixed-outcome fixtures. Full suite + lint green as the phase gate before commit.

---

## G. Implementation phases (backlog)

Each phase = its own branch work → tests → checkpoint (results, files changed, decisions, risks, docs) →
commit/PR. **Phases are executed one at a time, not in a single pass.**

- **Phase 1 — Data quality:** intraday volume profile (new `app/shortduration/volume_profile.py` + provider
  reuse + cache table + config); rename breadth→`watchlist_participation`; `MarketInternalsProvider` interface
  + mock + registry + regime confidence cap; `DataFreshnessPolicy` (`app/shortduration/freshness.py`) with
  per-state/track/use-case thresholds. API: `/market/internals`, `/market/participation`,
  `/short-duration/volume-profile/{symbol}`, `/short-duration/candidates/{id}/freshness`, `/configuration/freshness`.
- **Phase 2 — Strategy logic:** adaptive ORB buffer + anti-chase + confirmation modes; VWAP continuation-quality
  model (continuation/pullback/vwap-hold/structure/volume sub-scores, controlled reclaim); rebalance 0DTE
  weights → versioned config (`sd-0dte-v2`: 22/15/15/10/18/10/5/5). API: `/configuration/scoring/0dte`.
- **Phase 3 — Risk & trade management:** structure-aware `ExitPlan` (primary/secondary invalidation, premium
  backstop, PT1/PT2, time stop, momentum stop, EOD/expiration actions, rationale); `AccountStateProvider`
  (paper/live/fallback, verified flag) feeding sizing = equity − open risk − pending risk, subject to BP.
  API: `/short-duration/candidates/{id}/exit-plan`, `/account/state`.
- **Phase 4 — Paper analytics:** Book A (signal-validation) / Book B (account-executable) split;
  `executable_at_entry` + rejection reason; per-book rollups; opportunity-loss analytics. API:
  `/paper/performance?book=...`.
- **Phase 5 — News:** structured `CatalystEvent` classification layer (event types, direction, confidence,
  before/after values, mixed-outcome handling, source hierarchy, dedup grouping) alongside the keyword model,
  never bypassing deterministic gates. API: `/news/events/{symbol}`.
- **Phase 6 — UI & observability:** dashboard fields (quote age, freshness, model version, executable-at-entry,
  breakout buffer, VWAP-quality, exit-plan summary, internals-vs-participation with proxy label); new metrics;
  `/configuration/exit-policy`; docs incl. `METHODOLOGY.md`.
- **Phase 7 — Validation:** unit/integration/migration/scheduler/strategy/load/paper-sim; historical replay
  where data supports it.

## H. Assumptions (made to avoid blocking)

1. **No provider currently supplies true market internals** (A/D, TICK, up/down volume). Phase-1 ships the
   interface + a mock/`unavailable` implementation; a real feed is wired later. Regime uses participation as a
   *low-confidence* factor meanwhile.
2. **FMP intraday history is available** on the active plan (verified this session) for the volume profile;
   when a symbol has < min sessions, relvol is returned `estimated`/`unavailable`, never silently flat-equivalent.
3. **AccountState "live/verified" = Robinhood** later; Phase-3 ships paper + configured-fallback (both
   `unverified`) so nothing becomes live-executable from a constant.
4. **Weight versioning** uses a string id in config + persisted on each candidate; no weight history table needed yet.
5. **XSP / index options** remain out of scope for this phase (spreads-only, config-gated as documented).

## I. Rollback

Every phase is independently revertable (add-only schema, additive config with safe defaults, new modules).
Feature flags gate new behavior where it changes outputs (e.g. `SHORT_DURATION_USE_VOLUME_PROFILE`,
`SHORT_DURATION_SCORING_VERSION`) so a phase can be dark-launched and reverted without a migration rollback.

---

## Phase 1 — DELIVERED (data quality)

Shipped in tested increments:

1. **Intraday time-of-day volume profile** — `app/shortduration/volume_profile.py`. Per-minute median
   cumulative-volume baseline; honest `estimated`/`unavailable` fallback; feature-flagged. (9 tests)
2. **Real market internals + participation rename** — `app/providers/internals.py`
   (FMP sector breadth + UW market-tide + sector-flow). `BreadthProxy → WatchlistParticipation`;
   regime uses real internals as primary, caps confidence at 0.60 on proxy-only, no proxy hard-gate.
   `MarketInternalsProvider` capability + mock + config. (8 tests)
3. **Data-freshness policy** — `app/shortduration/freshness.py`. State/track/use-case budgets
   (broad 120s → armed-0DTE 8s → open-0DTE 5s); blocks stale/delayed/unknown-source trade-ready 0DTE.
   Attached to each candidate. (10 tests)

**Migration:** none required for Phase 1 — all new fields ride in existing JSON `payload`s or are
computed live / in-memory cached (the promoted-columns + payload pattern from §A). Migration `0004`
lands with Phase 2/3/4 (scoring version, exit plans, paper-book columns).

**APIs added:** `/market/internals`, `/market/participation`, `/short-duration/candidates/{id}/freshness`,
`/configuration/freshness`. **Tests:** 320 pass, lint clean. **Docs:** `METHODOLOGY.md` §3/§4/§10 updated.

---

## Phase 2 — IN PROGRESS (strategy logic)

1. **Rebalance + version the 0DTE scoring model** — DELIVERED. Weights moved into configuration
   (`scoring_0dte_weights` / `scoring_1_5dte_weights`) and versioned (`scoring_model_version`,
   `risk_policy_version`). v2 0DTE rebalance: price structure 20→**22**, contract liquidity 15→**18**,
   options-flow 15→**10** (a flow print is a hint, not the trade); 1–5DTE unchanged. The engine reads
   weights from config with a fixed ordered key list per model (a weight config can't silently drop or
   reorder a factor). Every candidate now records the model + risk-policy version it was scored under —
   promoted DB columns (**migration `0004`**) plus in the scorecard/payload. Composite total does not
   hide a bad component: risk / execution / liquidity / freshness stay separately inspectable and hard
   gates still apply. New endpoint `GET /short-duration/configuration/scoring`. (18 scoring tests pass;
   324 total.)
2. **Adaptive opening-range breakout** — pending.
3. **VWAP continuation-quality model** — pending.
