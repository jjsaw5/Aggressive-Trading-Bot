# Robinhood

Intended capabilities: **account equity, open option positions, option chains +
Greeks, market data.** Robinhood is the natural options-chain source for this
platform because FMP has no options and UW API access is a paid add-on.

## Status: skeleton (not built / not verified)

`app/providers/robinhood/client.py` conforms to the provider interfaces but its
methods raise `NotImplementedError`. `meta.verified = False` — the registry will
resolve it, but any call fails loudly until the integration is built.

## Two distinct Robinhood surfaces — do not conflate

1. **Robinhood MCP tools** (`get_option_chains`, `get_option_quotes`,
   `get_accounts`, `get_option_positions`, `place_option_order`, …) are
   available to the **assistant/agent during development**, not to the
   long-running Python service at runtime. They are useful for exploration and
   for validating field shapes, but the service cannot call them.
2. **Runtime access.** Robinhood has **no official public trading API**. The
   de-facto library is `robin_stocks` (unofficial). Using it may conflict with
   Robinhood's Terms of Service and carries account risk.

## Before enabling — checklist
- [ ] Confirm the current `robin_stocks` (or chosen client) method surface.
- [ ] Confirm the auth + MFA flow and store credentials via real secret
      management (never in the repo).
- [ ] Confirm Robinhood ToS permits programmatic access for your use.
- [ ] Map option-chain / Greeks / IV response fields; validate against a live
      account before trusting.
- [ ] Set `meta.verified = True` only once the above are done.

## Execution safety
This provider is **read-only** by design. Order placement is intentionally not
part of it and is gated separately by `app/modes/execution_guard.py` (automation
disabled by default, human approval always required).

## How to route (once built)
```env
PROVIDER_OPTIONS_CHAIN=robinhood
PROVIDER_BROKERAGE=robinhood
ROBINHOOD_USERNAME=...
ROBINHOOD_PASSWORD=...
ROBINHOOD_MFA_SECRET=...
```
