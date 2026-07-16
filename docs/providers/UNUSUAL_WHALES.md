# Unusual Whales (UW)

Research verified against the live official docs and published OpenAPI spec
(July 2026). Capabilities implemented: **options flow**, **IV-rank history**,
and **option chains**.

> **Option chains (live-validated 2026-07-16).** UW `option-contracts` returns
> real strikes, IV, OI, and NBBO bid/ask but **no greeks** — delta is computed
> via Black-Scholes from the underlying spot (`stock-state.close`) + per-contract
> IV. Strike/type/expiry are parsed from the OCC `option_symbol`. Expirations
> come from `expiry-breakdown` (centered on ~30 DTE); `option-contracts?expiry=`
> filters to one expiration (note: the param is `expiry`, not `expiry_date`).
> Current ATM IV for `get_iv_context` comes from `volatility/stats.iv`.
>
> Enable with `PROVIDER_OPTIONS_CHAIN=unusual_whales`. Real-data finding: on a
> $500–750 underlying, real strikes are $2.50–5 apart, so the narrowest
> defined-risk spread often exceeds a $100 per-trade cap — most mega-caps become
> untradeable at that budget, and the ones that fit are pushed to lower-POP OTM
> spreads. This is the true affordability constraint (the mock's $1 strikes hid
> it). Raise the per-trade budget or use a lower-priced universe for more setups.

> **Live-validated 2026-07-16** with a real key. Field mapping was corrected
> against the actual responses:
> - Flow (`/api/stock/{t}/flow-alerts`): real fields are `ticker`, `type`,
>   `strike`, `expiry`, `total_premium`, `total_size`, `open_interest`,
>   `has_sweep`, `all_opening_trades`, `total_ask_side_prem` /
>   `total_bid_side_prem`, `created_at`. Aggression (`at_ask`) and `sentiment`
>   are derived from the ask/bid-side premium split; sweeps from `has_sweep`;
>   opening from `all_opening_trades`.
> - IV history (`/api/stock/{t}/iv-rank`): the default returns only ~5 recent
>   rows — pass **`timespan=1Y`** for the full ~251-day series (fields `date`,
>   `close`, `volatility`, `iv_rank_1y`). The engine computes IV rank/percentile
>   from the `volatility` series; UW's own `iv_rank_1y` is available as a
>   cross-check.
>
> Enable with `PROVIDER_OPTIONS_FLOW=unusual_whales`,
> `PROVIDER_IV_HISTORY=unusual_whales`, and `UNUSUAL_WHALES_API_KEY` in the
> environment (never in committed code).

## Base URL & versioning
- Base: `https://api.unusualwhales.com`
- Path prefix: **`/api/...`** — there is **no `/v1` or `/v2` segment**. UW's own
  docs flag any `/api/v1/` or `/api/v2/` path as hallucinated.
- Docs: <https://api.unusualwhales.com/docs> · OpenAPI:
  <https://api.unusualwhales.com/api/openapi> · valid-vs-fake endpoint
  reference: <https://unusualwhales.com/skill.md>

## Authentication
- **Bearer token header only**: `Authorization: Bearer YOUR_KEY`.
- Query-string keys (`apiKey=` / `api_key=`) are explicitly invalid.
- API access is a **paid product, separate from the website subscription**
  (purchased at `unusualwhales.com/pricing?product=api`).

## Confirmed endpoints used
| Purpose | Path |
|---|---|
| Market-wide flow alerts | `GET /api/option-trades/flow-alerts` (supports `unusual=true`) |
| Per-ticker flow alerts | `GET /api/stock/{ticker}/flow-alerts` |
| IV history (for IV rank) | `GET /api/stock/{ticker}/iv-rank` — daily `date, close, volatility, iv_rank_1y` |

### Volatility endpoints (confirmed in the OpenAPI spec — for future use)
- `GET /api/stock/{ticker}/volatility/stats` — point-in-time `iv, iv_high, iv_low, iv_rank, rv, ...`
- `GET /api/stock/{ticker}/volatility/realized` — daily `implied_volatility` (≈IV30) + `realized_volatility`
- `GET /api/stock/{ticker}/volatility/term-structure` — per-expiry ATM IV
- `GET /api/stock/{ticker}/interpolated-iv` — per-tenor IV + 1y `percentile`

> IV rank is **computed by the engine** from the `volatility` series (or a
> realized-vol proxy) rather than trusting an opaque field — so ranks are
> consistent across providers. `volatility/stats.iv_rank` is available as a
> cross-check.

### Other confirmed endpoints (available for future capabilities)
- Recent flow: `GET /api/stock/{ticker}/flow-recent`
- Option chains: `GET /api/stock/{ticker}/option-chains`, `/option-contracts`, `/atm-chains`
- Greek exposure / GEX: `GET /api/stock/{ticker}/greek-exposure`, `/gex-levels`, `/greeks`
- Dark pool: `GET /api/darkpool/recent`, `GET /api/darkpool/{ticker}`
- Stock state / OHLC: `GET /api/stock/{ticker}/stock-state`, `/ohlc/{candle_size}`
- News: `GET /api/news/headlines`
- Congress: `GET /api/congress/recent-trades`
- WebSocket channels: `GET /api/socket/{channel}` (`flow_alerts`, `option_trades`, `price`, `gex`, `news`, …)

> **Do not hardcode** paths not confirmed above. UW-flagged fake paths include
> `/api/options/flow`, `/api/flow`, `/api/stock/{ticker}/flow`,
> `/api/unusual-activity`, and anything with `/api/v1|v2/`.

## Response field mapping
`app/providers/unusual_whales/client.py` maps flow-alert fields defensively
(tries known aliases, falls back to `None`). **Validate field names against the
live OpenAPI spec before production** — the confirmed facts are the paths + auth.

## Rate limits
- Per-token; surfaced on every response via headers:
  `x-uw-req-per-minute-remaining`, `x-uw-token-req-limit`, `x-uw-daily-req-count`
  (daily counter resets 8 PM ET). Exceeding returns **HTTP 429** (the client
  retries with backoff).
- Free API-key tier is very small (~5 req/min, ~50/month per third-party
  reporting); caps scale with the paid tier.

## Data delay
- Marketed real-time; REST-polling delay figure unconfirmed. Use the WebSocket
  channels for genuinely live flow.

## Licensing — important
- **Personal / internal use only.** Redistribution, reselling, sublicensing,
  scraping, and reverse-engineering are prohibited; UW states it will pursue
  legal action.
- **Do not expose raw UW data to third parties** without an enterprise
  redistribution license (`unusualwhales.com/enterprise`).

## How to enable
```env
PROVIDER_OPTIONS_FLOW=unusual_whales
UNUSUAL_WHALES_API_KEY=your_key
```
