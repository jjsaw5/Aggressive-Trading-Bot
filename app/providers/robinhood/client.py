"""Robinhood provider — account equity, open positions, and option chains.

Robinhood is the natural source for this platform's OPTIONS CHAIN + Greeks and
for real account state (equity, open option positions), since FMP does not
offer options and UW API access is a paid add-on.

ARCHITECTURE NOTE — two distinct Robinhood surfaces exist and must not be
conflated:

  1. The Robinhood MCP tools (get_option_chains, get_option_quotes,
     get_accounts, get_option_positions, place_option_order, ...) are available
     to the *assistant/agent* during development, NOT to this long-running
     Python service at runtime.
  2. This runtime provider must talk to Robinhood itself. Robinhood has no
     official public trading API; the de-facto library is `robin_stocks`
     (unofficial). Before enabling this provider you MUST confirm the current
     `robin_stocks` surface, auth/MFA flow, and Robinhood's terms of use — do
     not assume method names. See docs/providers/ROBINHOOD.md.

This class is a typed, interface-conforming SKELETON. Methods raise
`NotImplementedError` with explicit guidance until the integration is built and
verified. Order PLACEMENT is intentionally absent from this read-only provider;
execution is gated separately behind the automation kill-switch (app/modes).
"""

from __future__ import annotations

from app.domain.market import PriceHistory, Quote
from app.domain.options import IVContext, OptionChain
from app.providers.base import (
    BrokerageProvider,
    MarketDataProvider,
    OptionsChainProvider,
    ProviderMeta,
)

_META = ProviderMeta(
    name="robinhood",
    requires_auth=True,
    typical_delay="account data real-time; market data may be delayed. Verify.",
    rate_limit="unpublished; be conservative and cache aggressively.",
    licensing="Personal account use. No official public API; unofficial library use is "
    "at your own risk and may violate ToS. Verify before enabling.",
    docs_url="https://robin-stocks.readthedocs.io",
    verified=False,  # Not verified/built — do not route production traffic here yet.
)

_NOT_BUILT = (
    "RobinhoodProvider is a skeleton. Implement against a verified robin_stocks "
    "(or equivalent) client, confirm auth/MFA and Robinhood ToS, then set "
    "meta.verified=True. See docs/providers/ROBINHOOD.md."
)


class RobinhoodProvider(
    MarketDataProvider, OptionsChainProvider, BrokerageProvider
):
    meta = _META

    async def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError(_NOT_BUILT)

    async def get_price_history(self, symbol: str, lookback_days: int = 90) -> PriceHistory:
        raise NotImplementedError(_NOT_BUILT)

    async def get_option_chain(self, symbol: str, expirations: int = 4) -> OptionChain:
        raise NotImplementedError(_NOT_BUILT)

    async def get_iv_context(self, symbol: str) -> IVContext:
        raise NotImplementedError(_NOT_BUILT)

    async def get_account_equity(self) -> float:
        raise NotImplementedError(_NOT_BUILT)

    async def get_open_option_symbols(self) -> list[str]:
        raise NotImplementedError(_NOT_BUILT)
