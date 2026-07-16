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
from datetime import date

from app.domain.market import (
    CatalystEvent,
    EarningsEvent,
    Fundamentals,
    PriceHistory,
    Quote,
)
from app.domain.options import FlowAlert, IVContext, OptionChain, OptionMarkPoint


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


class CalendarProvider(Provider):
    @abc.abstractmethod
    async def get_earnings(self, symbol: str) -> EarningsEvent | None: ...

    @abc.abstractmethod
    async def get_catalysts(
        self, symbol: str, horizon_days: int = 21
    ) -> list[CatalystEvent]: ...


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
