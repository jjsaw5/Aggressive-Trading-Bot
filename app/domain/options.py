"""Options-domain models: contracts, greeks, flow, IV context."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from app.domain.enums import OptionType


class Greeks(BaseModel):
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None


class OptionContract(BaseModel):
    """A single option contract with pricing and liquidity fields.

    All liquidity fields are optional because providers differ; the liquidity
    filter treats missing data as a disqualifier rather than assuming quality.
    """

    symbol: str  # underlying
    option_symbol: str | None = None  # OCC symbol if available
    expiration: date
    strike: float
    option_type: OptionType

    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    mark: float | None = None

    volume: int | None = None
    open_interest: int | None = None
    implied_volatility: float | None = None
    greeks: Greeks = Field(default_factory=Greeks)

    as_of: datetime
    delayed_minutes: int = 0
    source: str = "unknown"

    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None and self.ask > 0:
            return round((self.bid + self.ask) / 2, 4)
        return self.mark or self.last

    @property
    def spread_pct(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            mid = self.mid
            if mid and mid > 0:
                return (self.ask - self.bid) / mid
        return None

    def dte(self, as_of: date) -> int:
        return (self.expiration - as_of).days


class OptionChain(BaseModel):
    symbol: str
    underlying_price: float | None = None
    contracts: list[OptionContract] = Field(default_factory=list)
    as_of: datetime
    source: str = "unknown"


class FlowAlert(BaseModel):
    """A single unusual-options-activity / flow print.

    Field names are provider-neutral; provider clients map their raw payloads
    onto this shape. Sentiment is normalized to [-1, 1] where >0 = bullish.
    """

    symbol: str
    option_type: OptionType | None = None
    strike: float | None = None
    expiration: date | None = None
    premium: float | None = None  # total notional premium of the print
    size: int | None = None  # contracts
    open_interest: int | None = None
    is_sweep: bool = False
    is_opening: bool | None = None  # opening vs closing, if known
    at_ask: bool | None = None  # aggressive buyer proxy
    sentiment: float | None = None  # normalized [-1, 1]
    ts: datetime
    source: str = "unknown"


class IVContext(BaseModel):
    """Implied-volatility context used to judge whether IV is favorable."""

    symbol: str
    iv30: float | None = None  # 30-day ATM implied vol
    iv_rank: float | None = None  # [0, 1]
    iv_percentile: float | None = None  # [0, 1]
    hv20: float | None = None  # 20-day realized vol
    term_structure_slope: float | None = None  # front-to-back IV slope
    as_of: datetime
    source: str = "unknown"

    @property
    def iv_hv_ratio(self) -> float | None:
        if self.iv30 and self.hv20 and self.hv20 > 0:
            return self.iv30 / self.hv20
        return None
