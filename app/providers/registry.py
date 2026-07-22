"""Provider registry: resolves a capability to a configured concrete provider.

Routing is driven entirely by settings (`PROVIDER_*`). Live providers are
imported lazily so the mock stack has zero third-party import cost, and a
misconfigured/unbuilt live provider fails loudly at resolution time rather
than silently returning bad data.

Every live provider is cached as a singleton (`@lru_cache`). This matters for
efficiency: a provider owns one pooled `httpx.AsyncClient`, so resolving a
capability must return the SAME instance each time rather than constructing a
fresh client (and leaking its connection pool) on every call. One vendor that
implements several capabilities (FMP: market data + fundamentals + calendar;
UW: flow + IV history + chain) is therefore backed by a single shared client.
"""

from __future__ import annotations

from functools import lru_cache

from app.config import ProviderName, settings
from app.logging_config import get_logger
from app.providers.base import (
    BrokerageProvider,
    CalendarProvider,
    EconomicCalendarProvider,
    FundamentalsProvider,
    HistoricalOptionsProvider,
    IntradayProvider,
    IVHistoryProvider,
    MarketDataProvider,
    NewsProvider,
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
def _fmp():
    # Cached so one pooled httpx client backs market-data + fundamentals +
    # calendar rather than a new client (and leaked pool) per resolver call.
    from app.providers.fmp.client import FMPProvider

    return FMPProvider()


@lru_cache
def _uw():
    # Cached so one pooled httpx client backs flow + IV-history + chain.
    from app.providers.unusual_whales.client import UnusualWhalesProvider

    return UnusualWhalesProvider()


@lru_cache
def _uw_historic():
    from app.providers.unusual_whales.historical import UWHistoricalOptionsProvider

    return UWHistoricalOptionsProvider()


@lru_cache
def _robinhood():
    # Cached so a single authenticated session is shared across the market-data,
    # options-chain, and brokerage capabilities (one login, not three).
    from app.providers.robinhood.client import RobinhoodProvider

    return RobinhoodProvider()


@lru_cache
def _benzinga():
    from app.providers.benzinga.client import BenzingaProvider

    return BenzingaProvider()


def _build(name: ProviderName, capability: str):
    if name == ProviderName.MOCK:
        return _mock()

    if name == ProviderName.FMP:
        # Key check stays OUTSIDE the cache so a missing key still fails loudly.
        if not settings.fmp_api_key:
            raise ProviderConfigError("FMP_API_KEY is not set")
        return _fmp()

    if name == ProviderName.UNUSUAL_WHALES:
        if not settings.unusual_whales_api_key:
            raise ProviderConfigError("UNUSUAL_WHALES_API_KEY is not set")
        return _uw()

    if name == ProviderName.ROBINHOOD:
        return _robinhood()

    if name == ProviderName.BENZINGA:
        if not settings.benzinga_api_key:
            raise ProviderConfigError("BENZINGA_API_KEY is not set")
        return _benzinga()

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


def historical_options_provider() -> HistoricalOptionsProvider:
    """Real per-contract historical marks for validating backtests. Gated on the
    UW historic entitlement being explicitly enabled AND the token present — off
    by default, so nothing calls the paid endpoint until it is licensed."""
    if not settings.uw_historic_enabled:
        raise ProviderConfigError(
            "UW_HISTORIC_ENABLED is false — real-mark backtesting is off until the "
            "UW historic API entitlement is confirmed on this token."
        )
    if not settings.unusual_whales_api_key:
        raise ProviderConfigError("UNUSUAL_WHALES_API_KEY is not set")
    return _uw_historic()


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


def intraday_provider() -> IntradayProvider:
    return _resolve(settings.provider_intraday, "intraday", IntradayProvider)


def news_provider() -> NewsProvider:
    return _resolve(settings.provider_news, "news", NewsProvider)


def econ_calendar_provider() -> EconomicCalendarProvider:
    return _resolve(
        settings.provider_econ_calendar, "econ_calendar", EconomicCalendarProvider
    )


@lru_cache(maxsize=1)
def _composite_internals():
    from app.providers.internals import CompositeMarketInternals

    return CompositeMarketInternals()


def market_internals_provider():
    """Real market internals via the FMP+UW composite, or the mock. Distinct from
    watchlist participation (a proxy over our own universe)."""
    if str(settings.provider_market_internals).lower() == "mock":
        return _mock()
    return _composite_internals()


@lru_cache(maxsize=1)
def _paper_account():
    from app.providers.account import PaperAccountState

    return PaperAccountState()


@lru_cache(maxsize=1)
def _fallback_account():
    from app.providers.account import ConfiguredFallbackAccountState

    return ConfiguredFallbackAccountState()


def account_state_provider():
    """The capital picture sizing reads. `paper` (default) tracks the simulated
    book; `fallback` is the configured constant. Both are UNVERIFIED — a live
    broker feed lands later and is the only `verified=True` source."""
    choice = str(settings.provider_account_state).lower()
    if choice == "fallback":
        return _fallback_account()
    return _paper_account()
