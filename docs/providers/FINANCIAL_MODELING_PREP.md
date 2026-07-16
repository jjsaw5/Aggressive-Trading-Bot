# Financial Modeling Prep (FMP)

Research verified against current official docs (July 2026). Capabilities
implemented: **market data, fundamentals, calendar**. FMP does **not** provide
options data.

## Base URL & versioning
- Base: `https://financialmodelingprep.com`
- Current family: **`/stable/...`** (actively documented). Legacy `/api/v3`,
  `/api/v4` remain live but FMP steers new builds to `stable`. Path shapes
  differ between families — do not mix.
- Docs: <https://site.financialmodelingprep.com/developer/docs/stable>

## Authentication
- API key as a **query parameter**: `?apikey=YOUR_KEY` on every request.
- A header form may also be accepted but is **unconfirmed** — use the query param.

## Confirmed endpoints used
| Purpose | Path |
|---|---|
| Quote | `GET /stable/quote?symbol=SYM` |
| Historical daily (full OHLCV) | `GET /stable/historical-price-eod/full?symbol=SYM` |
| Company profile | `GET /stable/profile?symbol=SYM` |
| Earnings calendar | `GET /stable/earnings-calendar` |
| Stock screener | `GET /stable/company-screener` |
| Market hours | `GET /stable/exchange-market-hours?exchange=NASDAQ` |

> Response **field names** are accessed defensively (`.get(...)`) in
> `app/providers/fmp/client.py`. Confirmed facts are the paths + auth; validate
> exact field names against a live response before trusting new fields. Missing
> fields degrade to `None` — never fabricated.

## Rate limits
- Free: **250 requests/day** (hard cap).
- Paid per-minute: Starter 300/min · Premium 750/min · Ultimate 3000/min.
- Separate 30-day bandwidth caps per tier.

## Data delay
- Quote marketed "real-time" ("up-to-the-minute"); some feeds may carry
  ~15–20 min exchange delay. **Per-tier delay table is not documented —
  confirm for your tier before trusting freshness.** The client sets
  `delayed_minutes=0` as a placeholder; adjust once confirmed.

## Tiers, pricing, options
- Annual-billed: Free $0 · Starter ~$22/mo · Premium ~$59/mo · Ultimate ~$149/mo.
  Month-to-month is higher (unconfirmed exact figures).
- **No options chains at any tier** — use Robinhood / Unusual Whales / a
  dedicated options vendor for chains + Greeks.

## Licensing
- Personal consumption on a paid plan is normal use.
- **Displaying/redistributing** FMP data to others requires a separate FMP
  *Data Display & Licensing Agreement*. Keep this a private tool, or license it.

## How to enable
```env
PROVIDER_MARKET_DATA=fmp
PROVIDER_FUNDAMENTALS=fmp
PROVIDER_CALENDAR=fmp
FMP_API_KEY=your_key
```
