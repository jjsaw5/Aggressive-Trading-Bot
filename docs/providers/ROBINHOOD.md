# Robinhood

Capabilities implemented: **market data** (quotes, historicals), **options
chain** (contracts + greeks + IV), and read-only **brokerage** (account equity,
open option positions). This is the platform's real options-chain source, since
FMP has no options and UW API access is a paid add-on.

## Status: implemented, source-grounded, NOT live-verified

`meta.verified = False`. The function/field mapping is grounded in **robin_stocks
3.4.0** source (module `robin_stocks.robinhood`), but it has **not been exercised
against a live account**. Flip to `True` only after a live auth + field smoke
test. Greeks/IV can be `null` for illiquid strikes; numeric fields arrive as
strings and are parsed defensively.

## Important — unofficial API

Robinhood has **no official public API**. `robin_stocks` is a reverse-engineered
library, and Robinhood's Terms of Service prohibit automated/programmatic access.
Use carries account risk and can break when Robinhood changes endpoints. Order
placement is deliberately **not** implemented here; execution is gated by the
execution guard (automation disabled by default).

## Confirmed functions used (robin_stocks 3.4.0)

| Purpose | Function |
|---|---|
| Login (headless MFA via pyotp TOTP) | `login(username, password, mfa_code=…, store_session=True)` |
| Quote | `stocks.get_quotes([symbol])` → `last_trade_price, bid_price, ask_price, previous_close` |
| Daily history | `stocks.get_stock_historicals(symbol, interval="day", span=…)` → `begins_at, open/high/low/close_price, volume` |
| Expirations | `options.get_chains(symbol)` → `expiration_dates` |
| Chain (merged mkt data) | `options.find_options_by_expiration(symbol, expirationDate=…)` → strike/type + `delta,gamma,theta,vega,implied_volatility,open_interest,volume,bid/ask/mark_price,occ_symbol` |
| Account equity | `profiles.load_portfolio_profile()` → `equity` |
| Open option positions | `options.get_open_option_positions()` → `chain_symbol, option_id, quantity, type` |

## Architecture

- `session.py` — lazy single login; every sync robin_stocks call is dispatched
  to a thread (`asyncio.to_thread`) to preserve the async provider interface.
  MFA is headless via `pyotp.TOTP(ROBINHOOD_MFA_SECRET).now()`.
- `mapping.py` — pure response-dict → domain-model parsing (unit-tested without
  the library or network; string→float casting, null-safe).
- `client.py` — the provider; centers option expirations on ~30 DTE so the
  standard 20–45 DTE selection window is covered.
- Registry caches one provider instance so all three capabilities share a single
  authenticated session (one login, not three).

## Gotchas (from robin_stocks 3.4.0)

- `get_option_market_data` wraps results in a nested list; `..._by_id` returns a
  flat dict. This provider uses `find_options_by_expiration` (flat list).
- Session token is pickled (`~/.tokens/…`); persist/mount it in containers or
  you re-auth (and re-MFA) every cold start. Token lifetime = `expiresIn` (24h);
  no auto-refresh — expect 401s after expiry and re-login.
- No throttling in the library and unpublished rate limits — avoid per-contract
  loops; batch and back off.

## Enable

```bash
pip install -e ".[robinhood]"     # robin-stocks + pyotp
```
```env
PROVIDER_MARKET_DATA=robinhood
PROVIDER_OPTIONS_CHAIN=robinhood
PROVIDER_BROKERAGE=robinhood
ROBINHOOD_USERNAME=...
ROBINHOOD_PASSWORD=...
ROBINHOOD_MFA_SECRET=...          # TOTP secret; required for headless MFA
```

## MCP vs runtime — do not conflate

The Robinhood MCP tools (available to the assistant during development) are
separate from this runtime provider, which must talk to Robinhood itself via
robin_stocks. The MCP tools are useful for validating field shapes during dev.
