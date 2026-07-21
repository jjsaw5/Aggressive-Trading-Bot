"""Abstract provider capability interfaces.

Capabilities are intentionally narrow so a single vendor can implement several
(e.g. FMP implements market-data + fundamentals + calendar) while another
implements only one (Unusual Whales -> options flow).

`ProviderMeta` forces each concrete provider to declare the operational facts
that MUST be confirmed against official docs before enabling it in production:
auth requirement, data delay, rate limit, and licensing note. This makes the
"verify before integrating" rule a property of the code, not just a checklist.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import date, datetime

from app.domain.market import (
    CatalystEvent,
    EarningsEvent,
    Fundamentals,
    PriceHistory,
    Quote,
)
from app.domain.options import (
    FlowAlert,
    IVContext,
    IVHistory,
    OptionChain,
    OptionMarkPoint,
)
from app.domain.internals import MarketInternals
from app.domain.shortduration import EconomicEvent, IntradayBar, NewsItem


@dataclass(frozen=True)
class ProviderMeta:
    name: str
    requires_auth: bool
    # Human-readable, sourced-from-docs facts. `None` = unconfirmed.
    typical_delay: str | None
    rate_limit: str | None
    licensing: str | None
    docs_url: str | None
    verified: bool = False  # True only once endpoints confirmed against docs


class Provider(abc.ABC):
    """Base class carrying provider metadata."""

    meta: ProviderMeta

    @property
    def name(self) -> str:
        return self.meta.name


class MarketDataProvider(Provider):
    @abc.abstractmethod
    async def get_quote(self, symbol: str) -> Quote: ...

    @abc.abstractmethod
    async def get_price_history(
        self, symbol: str, lookback_days: int = 90
    ) -> PriceHistory: ...


class FundamentalsProvider(Provider):
    @abc.abstractmethod
    async def get_fundamentals(self, symbol: str) -> Fundamentals: ...


class OptionsChainProvider(Provider):
    @abc.abstractmethod
    async def get_option_chain(
        self, symbol: str, expirations: int = 4
    ) -> OptionChain: ...

    @abc.abstractmethod
    async def get_iv_context(self, symbol: str) -> IVContext: ...

    async def get_option_chain_for_expirations(
        self, symbol: str, expirations: list[date]
    ) -> OptionChain:
        """Fetch a chain covering SPECIFIC expiration dates — for monitoring held
        positions whose expiry may fall outside the default ~30-DTE window.

        Default falls back to the standard chain (best effort); providers that can
        target expirations directly should override this for correctness."""
        return await self.get_option_chain(symbol)


class OptionsFlowProvider(Provider):
    @abc.abstractmethod
    async def get_flow_alerts(
        self, symbol: str | None = None, unusual_only: bool = True, limit: int = 100
    ) -> list[FlowAlert]: ...


class HistoricalOptionsProvider(Provider):
    """Historical per-contract option marks for production backtesting.

    This is the slot a true historical options-quote feed plugs into (e.g. a UW
    historical option-trades add-on, Polygon/Databento/ORATS, or a broker's
    historical option data). When configured, the backtester replays these real
    marks directly; otherwise it falls back to repricing real underlying paths
    with Black-Scholes (see app/backtest/historical.py).

    No such feed is confirmed/available in the current stack, so this interface
    intentionally has no built implementation yet — do not fabricate one.
    """

    @abc.abstractmethod
    async def get_option_mark_series(
        self, option_symbol: str, start: date, end: date
    ) -> list[OptionMarkPoint]: ...


class IVHistoryProvider(Provider):
    """Historical implied-volatility series for computing true IV rank/percentile.

    This is the slot a real IV-history feed plugs into. When no such feed is
    configured, the IV-context builder falls back to a realized-volatility proxy
    computed from real underlying price history (clearly labeled). Do not
    fabricate an endpoint — implement only against a verified source.
    """

    @abc.abstractmethod
    async def get_iv_history(self, symbol: str, lookback_days: int = 365) -> IVHistory: ...


class CalendarProvider(Provider):
    @abc.abstractmethod
    async def get_earnings(self, symbol: str) -> EarningsEvent | None: ...

    @abc.abstractmethod
    async def get_catalysts(
        self, symbol: str, horizon_days: int = 21
    ) -> list[CatalystEvent]: ...


class IntradayProvider(Provider):
    """Intraday OHLCV bars — the base layer for VWAP, opening range, and
    relative volume. Distinct from `MarketDataProvider.get_price_history`, which
    is daily EOD only."""

    @abc.abstractmethod
    async def get_intraday_bars(
        self, symbol: str, *, interval: str = "1min", session_date: date | None = None,
        from_date: date | None = None, to_date: date | None = None,
    ) -> list[IntradayBar]:
        """Chronological bars for the given interval ("1min"|"5min"). When
        `session_date` is None, returns the most recent session available. Pass
        `from_date`/`to_date` for a multi-session range (used to build the
        historical intraday volume profile); `session_date` takes precedence."""
        ...


class NewsProvider(Provider):
    """Headline news with latency lineage. Producers must set `received_ts`;
    `source_ts`/`provider_ts` are set when the feed supplies them so end-to-end
    latency can be measured."""

    @abc.abstractmethod
    async def get_news(
        self,
        symbols: list[str] | None = None,
        *,
        limit: int = 50,
        since: datetime | None = None,
    ) -> list[NewsItem]: ...


class EconomicCalendarProvider(Provider):
    """Scheduled macro releases (CPI, FOMC, NFP, ...). Separate from earnings —
    these gate whole-market restricted windows, not single symbols."""

    @abc.abstractmethod
    async def get_economic_events(
        self, *, from_date: date | None = None, to_date: date | None = None
    ) -> list[EconomicEvent]: ...


class BrokerageProvider(Provider):
    """Account/positions read + gated order placement.

    Order placement is deliberately NOT part of the read path. Callers must go
    through the execution guard (see app/modes) which enforces the automation
    kill-switch — a brokerage provider must never place an order on its own.
    """

    @abc.abstractmethod
    async def get_account_equity(self) -> float: ...

    @abc.abstractmethod
    async def get_open_option_symbols(self) -> list[str]: ...

    async def get_option_positions(self) -> list:
        """Open option positions grouped into structures, as
        ``list[(symbol, list[ImportedLeg])]`` for the position importer. Default
        is empty; override in a broker that can read positions with cost basis."""
        return []


class MarketInternalsProvider(Provider):
    """Real market-wide internals (breadth of price + flow). Distinct from
    watchlist participation, which is a proxy over our own universe. A provider
    returns whatever it can source and marks the rest unavailable — it must never
    fabricate a neutral/bullish value for a field it cannot read."""

    @abc.abstractmethod
    async def get_market_internals(self, *, now: datetime | None = None) -> MarketInternals: ...
