# Short-Duration Options Engine — Methodology & Data Review

**Scope:** 0DTE, 1–5DTE, and swing · **Instruments:** Equity / ETF options ·
**Status:** Research & paper only · **Account:** ~$2,000 aggressive options account

> **Read first.** This system is *decision support*. It surfaces and ranks trade ideas; it
> **never places live orders**. Every execution path is denied by default behind a two-key safety
> gate (§13). Suggested plays are meant to be reviewed by a human and, if chosen, entered manually
> on the broker. This document is for an external options trader to review the **logic and the
> data** — not to endorse any specific trade.

A shareable web version of this report is also published as an Artifact.

---

## 1. What it is, and the one principle everything hangs on

The engine watches a universe of liquid, actively-optioned names and, on a fixed cadence during
regular trading hours, tries to answer one question per symbol: **is there a valid market setup
right now, and if so, is a short-dated option a sensible way to express it?**

The ordering is deliberate and load-bearing:

> **Setup-first principle.** The system must first identify a valid market setup. Only after the
> setup is confirmed does it decide whether a short-duration option is an appropriate vehicle.
> **News can create a candidate, but news alone never approves a trade** — price, volume, or flow
> confirmation is required before it counts for much (§6–§7).

Three tracks run off the same pipeline:

| Track | Horizon | Setups it looks for | Primary data |
|---|---|---|---|
| **0DTE** | Same-day expiry | Opening-range breakout, VWAP-trend continuation | 1-min intraday bars |
| **1–5DTE** | 1–5 days | Daily-trend continuation, catalyst continuation | Daily bars + intraday confirm |
| **Swing (core)** | Weeks | The original multi-tier scanner | Daily / fundamentals |

**The pipeline, every symbol, every scan:**

```
Regime → Detect → Score → Contract → Gates → Lifecycle
(market  (setup   (weighted (defined- (time/  (watch →
 gate)    conds)   model)    risk)     risk)    arm → …)
```

---

## 2. Data sources & data points

Every provider is behind a capability interface, so a source can be swapped without touching the
logic. Each field below is a real data point the engine consumes.

| Provider | Feeds | Data points consumed | Status |
|---|---|---|---|
| **Financial Modeling Prep** | Quotes, intraday bars, daily history, fundamentals, earnings, econ calendar | price, prev_close, volume · 1-min & 5-min OHLCV · 252-day daily OHLCV · market_cap, sector, float · earnings date/time · econ event impact/estimate/actual | live · verified |
| **Unusual Whales** | Options flow, option chains, IV context | per-alert: type, strike, expiry, premium, size, OI, is_sweep, is_opening, at_ask, sentiment · full chain: bid/ask, OI, volume, IV, greeks (Δ Γ Θ V ρ) · iv30, iv_rank | live · verified |
| **Benzinga** | News | headline, summary, source, channels, published timestamp, tickers | live · verified |
| **Robinhood** | Brokerage positions / account (optional) | positions, greeks, account equity — via the Claude connector today, not the app runtime | not wired in-app |

**Honest data gaps:**

- **1-minute intraday** requires an FMP plan tier that includes intraday charts. On a free/basic key
  it returns HTTP 402 and the intraday layer (VWAP / opening range / relative volume) is empty — the
  0DTE track cannot produce real setups until that plan is active.
- **Provider greeks are sometimes degenerate** for single-stock 0DTE chains (delta reported as 0 or
  1). The selector falls back to a moneyness (strike-vs-spot) proxy so those liquid names aren't
  wrongly dropped (§9).
- **Account equity** used for sizing is a static `$2,000` constant, not a live broker balance.

---

## 3. Market-regime engine — a transparent rules gate

Before any symbol is scored, the engine reads the broad tape and classifies a **regime**. The regime
doesn't pick trades; it sets two switches — whether new trades are allowed at all, and whether size
should be reduced — and feeds one scoring factor. All rules are explicit thresholds, nothing learned
or opaque.

**Inputs:**

- **Index votes** — SPY, QQQ, IWM. Each casts a directional vote requiring *both* a move of at least
  `±0.10%` on the day *and* agreement with its own VWAP side.
- **Market internals (v2, real)** — a composite [0,1] breadth score from FMP sector breadth (advancing
  sectors) + Unusual Whales market tide (net call/put premium) + sector-flow breadth. This is the primary
  breadth signal when available. Classic NYSE A/D-issues / TICK / up-down volume aren't on the current
  keys and are modeled as `unavailable`, never faked.
- **Watchlist participation (proxy)** — the share of *our tracked universe* above session VWAP. Used only
  as a low-confidence contextual factor: it does **not** hard-gate the trend and **caps regime confidence
  at 0.60** when it's the only breadth signal available.
- **Volatility reading** — SPY IV rank, in [0,1].
- **Event proximity** — minutes until the next scheduled economic event and its impact tier.

**How inputs map to state & gates:**

| Condition | Regime | New trades | Size |
|---|---|---|---|
| High-impact event within 15 min | Macro-event-driven | **blocked** | reduced |
| Net index vote ≥ +2 & breadth ≥ 0.60 | Bull trend (high-vol variant if IV-rank ≥ .60) | allowed | normal |
| Net index vote ≤ −2 & breadth ≤ 0.40 | Bear trend | allowed | normal |
| No decisive votes, IV-rank ≥ .60 | High-vol chop | allowed | reduced |
| No decisive votes, IV-rank ≤ .35 | Low-vol compression | allowed | normal |
| Mixed / indecisive | Range-bound | allowed | normal |

**Gates:** `allow_new_trades = not in_blackout`. Only a high-impact event ≤15 min (or an explicit
restriction) blocks entries. `reduce_size` trips on high IV-rank, high-vol-chop / macro-event
regimes, or a high-impact event ≤60 min. Regime **confidence** starts at 0.35, adds up to +0.50 for
index agreement and +0.15 if breadth data exists, capped at 0.50 for range-bound / chop regimes.

> **v2 update — real internals now separate the proxy.** A real market-internals composite (sector
> breadth + options-flow tide) is now the primary breadth signal. Watchlist participation is explicitly
> a proxy, no longer called "market breadth", never hard-gates the trend, and caps confidence at 0.60
> when it's the only signal. Fields no provider can source (A/D issues, TICK) are surfaced as
> `unavailable`, never faked. `GET /market/internals` and `/market/participation` expose both.

---

## 4. Intraday primitives

All three are pure functions over RTH (09:30–16:00 ET) 1-minute bars.

- **VWAP** — typical price per bar is `(high + low + close) / 3`, volume-weighted cumulatively.
  Returns nothing if session volume is zero.
- **Opening range** — high/low of the first 15 minutes (09:30–09:45 ET). Reported *only after the
  window closes* — no premature breakouts off a half-formed range.
- **Relative volume (v2, time-of-day profile)** — `relvol = today's cumulative volume ÷ historical
  **median** cumulative volume at this minute-of-session`, built from the last N completed sessions
  (median resists event-day outliers). This replaces the old flat proration and captures the real
  U-shaped intraday curve. Under the minimum usable sessions it degrades to a **labeled `estimated`**
  flat fallback (or `unavailable`) — never silently presented as equivalent quality, and a missing
  reading can never inflate a score. The method + estimated flag are carried on every levels reading.

> **Data note.** Building the profile needs multi-session intraday history. On the current FMP tier,
> 1-minute history is ~1 session but 5-minute is longer, so the baseline is built from 5-minute bars;
> when history is still short the reading is honestly marked `estimated` until a deeper-history intraday
> source (or FMP tier) is available.

---

## 5. Strategy detectors — exact trigger conditions

A detector emits a candidate only when its setup conditions hold. Some checks are **hard gates**
(fail → no candidate); others are **score bonuses**. A bullish setup is hard-blocked in a bear-trend
regime and vice-versa; but `allow_new_trades = false` only annotates a candidate, it doesn't suppress
the research. No detector computes price targets.

### 0DTE · Opening-Range Breakout

- Direction: **bullish** if last ≥ OR-high × 1.0005, **bearish** if last ≤ OR-low × 0.9995 (must
  clear the range by ≥0.05%).
- Close-not-wick: the last completed 1-min bar must close beyond the level.
- VWAP alignment (**gate**): price must be on the trade's side of VWAP.
- Relative volume: if known and `< 1.3×`, rejected (a missing relvol does not block).

### 0DTE · VWAP-Trend Continuation

- Needs ≥10 of the last 20 one-minute bars; fits a linear slope of closes.
- Direction: bullish if price > VWAP *and* per-bar slope ≥ `+0.02%`; bearish if the mirror.
- Pullbacks-held-VWAP (**gate**): no bar in the window closed on the wrong side of VWAP.
- Volume expansion (last third > first third) is a score bonus, not a gate.

### 1–5DTE · Trend Continuation

- Direction from a daily price-action analyzer (SMA-20 vs SMA-50 structure); neutral is rejected.
- Daily-strength floor (**gate**): analyzer score ≥ `0.55`.
- Intraday alignment: if the symbol's VWAP side is known, it must agree with the daily direction.

### 1–5DTE · Catalyst Continuation

- Catalyst required (**gate**): a fresh headline (≤48h) *or* a scheduled event.
- Price follow-through (**gate**): net 3-session move ≥ `±2%` in the catalyst's direction.
  **Direction comes from the realized price move, not the headline's tone.** A catalyst with no price
  continuation is explicitly "not actionable."
- Volume confirm (**gate**, when ≥20 daily bars exist): last-3-session volume > 20-day average.

---

## 6. Scoring models — two weighted books

A confirmed setup is scored by a per-track weighted model. Each factor is a quality reading in [0,1]
times its weight; weights sum to **100**. The score ranks the board and sets the candidate's state.

- Missing factor raw value: `0.25` — flagged, *never* neutral 0.5.
- Data-quality tempering: `confidence = score × (0.6 + 0.4 × data_quality)` — can only lower.
- Score to **Arm**: `0.70` · Score to **Watchlist**: `0.50`.
- **Weights are configuration, not code** — `scoring_0dte_weights` / `scoring_1_5dte_weights`, exposed
  read-only at `GET /short-duration/configuration/scoring`. Every candidate records the
  `scoring_model_version` and `risk_policy_version` it was scored under (promoted DB columns) so a
  book can be filtered and A/B-compared across weightings. Current: `sd-scoring-2026.07-v2`.

**0DTE model** (v2) — weighted toward live intraday structure and executable liquidity; raw flow
trimmed (a flow print is a hint, not the trade):

| Factor | Wt | Driven by |
|---|---:|---|
| Intraday price structure | 22 | VWAP side + opening-range break (fraction of checks passed) |
| Market & sector alignment | 15 | Regime + breadth; contra-trend hard-capped at 0.10 |
| Relative volume & momentum | 15 | relvol (1×→0, 3×→1) averaged with day-change momentum |
| Options-flow quality | 10 | Decayed flow sentiment & confidence (§8) |
| Contract liquidity | 18 | Near-ATM spread (50%) + OI (30%) + volume (20%) |
| Volatility suitability | 10 | IV rank curve — best at 0.2–0.5, penalized when rich |
| Catalyst & news | 5 | News score (floored 0.3) + scheduled catalyst |
| Risk / reward | 5 | Reward-to-risk of the sized structure (2:1 → 1.0) |
| **Total** | **100** | |

The composite total never hides a bad component: risk quality, execution quality, contract liquidity,
and data freshness stay individually inspectable on the scorecard, and a candidate still fails its
**hard gates** (freshness, liquidity floor, defined-risk cap) regardless of how high the total is.

**1–5DTE model** — weighted toward daily trend and multi-session flow:

| Factor | Wt | Driven by |
|---|---:|---|
| Daily & intraday trend | 20 | Daily price-action score (needs ≥50 closes) |
| Catalyst & news | 15 | News score + scheduled catalyst |
| Multi-session flow | 15 | Print count, repeated strikes, opposing-flow penalty |
| Market & sector alignment | 10 | Regime + breadth |
| Volatility suitability | 10 | IV rank curve |
| Contract liquidity | 10 | Near-ATM spread / OI / volume |
| Technical-entry quality | 10 | Intraday VWAP alignment (0.7 aligned / 0.3 not / 0.4 unknown) |
| Risk / reward | 10 | Reward-to-risk of the sized structure |
| **Total** | **100** | |

**Confidence.** The board ranks on the raw normalized score. Separately, `confidence = score ×
(0.6 + 0.4 × data_quality)` — missing/stale inputs can only *lower* confidence, never inflate it.
Data quality is the fraction of input checks passed (fresh quote ≤120s, VWAP present, chain present,
IV rank present, plus track-specific checks).

---

## 7. News scoring — and why a headline can't run away with the score

News is scored by its own 7-factor model into a single [0,1] value, which then enters the main model
*only* through the catalyst factor (weight 5 for 0DTE, 15 for 1–5DTE). A perfect news score
contributes at most 5 or 15 of 100 points — the setup still has to carry the trade.

| Factor | Wt | How it's read |
|---|---:|---|
| Materiality | 25 | Count of material keywords (earnings, guidance, FDA, M&A, upgrade/downgrade, halt…) |
| Source authority | 20 | Tiered: Reuters/Bloomberg 1.0 → Benzinga 0.9 → unknown 0.35 (never 0) |
| Novelty | 20 | 1.0 fresh, collapses to 0.15 if a near-duplicate |
| Relevance | 15 | 1.0 exact ticker · 0.6 has-a-ticker · 0.3 none |
| Price confirmation | 10 | Day-move aligned with the headline direction |
| Volume confirmation | 5 | Relative volume above 1× |
| Flow confirmation | 5 | Decayed flow agrees with the headline direction |
| **Total** | **100** | |

Direction is inferred from bullish/bearish language when the item has none. Duplicates are caught by
Jaccard token overlap ≥ `0.6` against recently-seen headlines (processed newest-first), so a recycled
story loses its novelty weight. **20 of the 100 news points are confirmation** (price/volume/flow) —
an unconfirmed headline scores materially lower.

---

## 8. Options-flow decay — recent prints dominate

Unusual-options prints are age-weighted so a fresh sweep counts far more than a half-hour-old one.
Sentiment is the weighted mean of per-print sentiment; confidence scales that magnitude by the
freshest bucket's weight and is **halved when opposing flow is present**.

| Print age | Weight | Reading |
|---|---:|---|
| 0–2 min | 1.00 | Live |
| 2–5 min | 0.80 | Fresh |
| 5–15 min | 0.50 | Recent |
| 15–30 min | 0.25 | Fading |
| > 30 min | 0.10 | Context only |

Direction is used *only* to detect opposing flow (a print against the setup beyond ±0.3 sentiment) —
never to cherry-pick confirming prints. Repeated strikes add a small aligned bonus.

---

## 9. Contract selection & sizing — defined risk, always

For a confirmed setup the engine builds every viable **defined-risk** expression and offers them as
separate, individually-scored plays so a human can choose the structure:

- A near-ATM **single leg** (Long Call / Long Put) — max loss = the debit paid, and
- A **defined-risk debit vertical** (Call Debit Spread / Put Debit Spread) sized to the cap.

Contracts are chosen in a delta band (≈0.35–0.68, target 0.50) with liquidity gates (spread ≤
12–15%, OI/volume floors). When provider greeks are degenerate, selection falls back to a moneyness
band (±3% for 0DTE, ±5% for 1–5DTE). Names are shown with their **Robinhood order-ticket labels** so
a suggestion maps 1:1 to what you'd build.

**Per-trade risk cap** = `min(risk% × equity, absolute $ cap)`. Defaults: 0DTE 3% → $60, 1–5DTE 5% →
$100, hard ceiling $100. A setup with no defined-risk structure that fits is **rejected with a stated
reason**, never force-fit. A "paper verification" mode can lift the cap and size a single contract so
the full mix is visible for forward-testing — it changes sizing only, never the live-execution gate.

---

## 10. Risk & trade management

Entry is gated on time-of-day and account state; exits are pre-planned at contract selection.

| Guardrail | Rule |
|---|---|
| Opening block | No entries in the first 5 minutes (09:30–09:35 ET) |
| 0DTE entry cutoff | No new 0DTE entries after 15:00 ET |
| 0DTE force-close | Open 0DTE flattened by the 15:45 ET review — never rides into expiry |
| Profit target / stop | Default 50% of max profit / 50% of max loss |
| Time stop | Same-day for 0DTE; ≤1 DTE for 1–5DTE |
| Daily-loss halt | Stop new trades past −5% on the day |
| Consecutive-loss halt | Stop after 2 straight losses |
| Concurrency | Max 2 concurrent short-duration positions |
| Freshness gate (v2) | State/track-aware: broad 120s → watchlist 30s → **armed 0DTE 8s** → open 0DTE 5s. A trade-ready 0DTE candidate is blocked on a quote older than its budget, on delayed data, or on an unknown source. `GET /configuration/freshness` and `/candidates/{id}/freshness`. |

Sizing is anchored to a `$2,000` account, max 15% total account risk. On a book that small, the $60
0DTE cap is deliberately protective — one bad 0DTE is 3% of capital.

---

## 11. Candidate lifecycle — a transparent state machine

Every candidate carries a full, timestamped transition trail. Nothing is silently dropped — a
rejection records its reason.

```
Detected → Evaluating → Watchlist / Armed → Triggered → Proposed → Approved → Open → Managing → Closed
                                                          (human)   (human)
```

Branches: **Rejected** (e.g. no defined-risk contract fits the cap) and **Expired**. The paper path
can go Triggered → Open directly. The board ranks by **actionability** — ready-to-trade first, then
watchlist, with rejected/closed collapsed at the bottom — not by scan time.

---

## 12. Symbol search — an on-demand deep-dive on any ticker

Typing a ticker runs the whole pipeline on that one name and returns a live report: quote, intraday
levels, IV, unusual flow (call/put lean, premium, sentiment), news, catalysts, fundamentals, and
suggested plays from all three tracks — ranked, with defined-risk structures. Same setup-first rule:
the plays come from the engines, the news is context.

---

## 13. Safety architecture — why this can't place a live order

Execution is behind a **two-key gate**: it authorizes only when the trading mode is `automation`
*and* automation is explicitly armed. Both default off, so every execution request is **denied** and
no order path is even wired. The system's job is to research, rank, and propose; a human approves and
enters manually. Approvals are explicit and attributed.

---

## 14. Known limitations — and the questions we most want answered

We'd rather over-disclose. These are the design choices we're least sure about and would value a
professional options trader's read on.

1. **Breadth internals (v2 — partially addressed).** Real internals (sector breadth + options-flow
   tide) now drive the regime, and the VWAP-share reading is demoted to a capped, non-gating proxy.
   Still missing on current keys: NYSE A/D-issues, TICK, up/down volume, new highs/lows (modeled as
   `unavailable`). Is sector-breadth + flow-tide sufficient, or do you consider A/D/TICK essential?
2. **Relative volume (v2 — addressed).** Now a historical time-of-day **median** cumulative-volume
   profile, with an honest `estimated`/`unavailable` fallback. Open question: the current FMP tier
   limits intraday history, so profiles often run `estimated` — worth upgrading the data source?
3. **Scoring weights are hand-set, not fit.** The 100-point weights (§6) and thresholds (arm 0.70,
   watchlist 0.50) are informed guesses, unvalidated against outcomes. Are the *relative* weightings
   sensible for 0DTE vs 1–5DTE? What would you move?
4. **Contract selection band.** Delta 0.35–0.68 target 0.50, and the moneyness fallback. Right band
   for 0DTE? Should we prefer spreads over naked longs by default to cap theta/vega?
5. **Exit defaults.** 50% profit / 50% stop, 0DTE force-close 15:45, no computed price targets. Are
   fixed-percentage exits appropriate for 0DTE gamma, or should exits be structure/greek-aware?
6. **Risk caps for a $2k account.** 3% ($60) 0DTE, 5% ($100) 1–5DTE, 2 concurrent, −5% daily halt.
   Too tight, too loose, or about right for an aggressive small account?
7. **News weighting.** News caps at 5 (0DTE) / 15 (1–5DTE) of 100 points, confirmation-gated. Is that
   the right ceiling, or should confirmed, high-materiality news move the needle more?
8. **What are we missing?** Any setup, filter, or risk control you'd expect in a serious
   short-duration options process that isn't here.

> **The ask.** Read this as the specification of a decision-support tool, not a track record — it has
> placed no live trades. We want your judgment on whether the *logic and data are sound* before we
> forward-test it harder on paper. Blunt feedback is the goal.
