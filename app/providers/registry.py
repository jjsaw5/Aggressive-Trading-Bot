"""Provider registry: resolves a capability to a configured concrete provider.

Routing is driven entirely by settings (`PROVIDER_*`). Live providers are
imported lazily so the mock stack has zero third-party import cost, and a
misconfigured/unbuilt live provider fails loudly at resolution time rather
than silently returning bad data.
"""

from __future__ import annotations

from functools import lru_cache

from app.config import ProviderName, settings
from app.logging_config import get_logger
from app.providers.base import (
    BrokerageProvider,
    CalendarProvider,
    FundamentalsProvider,
    IVHistoryProvider,
    MarketDataProvider,
    OptionsChainProvider,
    OptionsFlowProvider,
)
from app.providers.mock import MockProvider

log = get_logger(__name__)


class ProviderConfigError(RuntimeError):
    pass


@lru_cache
def _mock() -> MockProvider:
    return MockProvider()


@lru_cache
def _robinhood():
    # Cached so a single authenticated session is shared across the market-data,
    # options-chain, and brokerage capabilities (one login, not three).
    from app.providers.robinhood.client import RobinhoodProvider

    return RobinhoodProvider()


def _build(name: ProviderName, capability: str):
    if name == ProviderName.MOCK:
        return _mock()

    if name == ProviderName.FMP:
        from app.providers.fmp.client import FMPProvider

        if not settings.fmp_api_key:
            raise ProviderConfigError("FMP_API_KEY is not set")
        return FMPProvider()

    if name == ProviderName.UNUSUAL_WHALES:
        from app.providers.unusual_whales.client import UnusualWhalesProvider

        if not settings.unusual_whales_api_key:
            raise ProviderConfigError("UNUSUAL_WHALES_API_KEY is not set")
        return UnusualWhalesProvider()

    if name == ProviderName.ROBINHOOD:
        return _robinhood()

    raise ProviderConfigError(f"Unknown provider {name!r} for {capability}")


def _resolve(name: ProviderName, capability: str, iface: type):
    provider = _build(name, capability)
    if not isinstance(provider, iface):
        raise ProviderConfigError(
            f"Provider {name.value!r} does not implement {capability} "
            f"({iface.__name__})"
        )
    return provider


def market_data_provider() -> MarketDataProvider:
    return _resolve(settings.provider_market_data, "market_data", MarketDataProvider)


def fundamentals_provider() -> FundamentalsProvider:
    return _resolve(settings.provider_fundamentals, "fundamentals", FundamentalsProvider)


def options_chain_provider() -> OptionsChainProvider:
    return _resolve(settings.provider_options_chain, "options_chain", OptionsChainProvider)


def options_flow_provider() -> OptionsFlowProvider:
    return _resolve(settings.provider_options_flow, "options_flow", OptionsFlowProvider)


def calendar_provider() -> CalendarProvider:
    return _resolve(settings.provider_calendar, "calendar", CalendarProvider)


def iv_history_provider() -> IVHistoryProvider | None:
    """Optional. Returns None when unconfigured — the IV-context builder then
    falls back to a realized-volatility proxy from real price history."""
    if settings.provider_iv_history is None:
        return None
    return _resolve(settings.provider_iv_history, "iv_history", IVHistoryProvider)


def brokerage_provider() -> BrokerageProvider:
    return _resolve(settings.provider_brokerage, "brokerage", BrokerageProvider)
