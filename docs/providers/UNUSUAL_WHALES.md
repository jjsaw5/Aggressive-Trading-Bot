# Unusual Whales (UW)

Research verified against the live official docs and published OpenAPI spec
(July 2026). Capability implemented: **options flow**.

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
