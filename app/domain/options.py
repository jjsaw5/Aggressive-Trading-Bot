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


class OptionMarkPoint(BaseModel):
    """A single historical mark for one option contract, for backtest replay."""

    option_symbol: str
    ts: datetime
    mark: float  # per-share settlement/close mark
    underlying_price: float | None = None
    implied_volatility: float | None = None
    source: str = "unknown"


class OptionMinuteBar(BaseModel):
    """One minute of a single option contract's trading (Phase 2).

    From UW `/api/option-contract/{id}/intraday`. Bars exist only for minutes
    that actually traded, so a session is sparse: ~123 bars over a 390-minute
    session is normal. A gap means "no print", which the replay holds through —
    it never interpolates a mark it did not observe.

    `open/high/low/close` are TRADE prices, not quotes. The bid/ask split here is
    a classification of where each trade printed relative to the book, not an
    NBBO. `effective_bid`/`effective_ask` derive a realistic round-trip cost from
    what actually transacted; they are labeled distinctly from NBBO for exactly
    that reason.
    """

    option_symbol: str
    start_time: datetime  # bar open, UTC, left-edge labeled
    open: float
    high: float
    low: float
    close: float
    avg_price: float | None = None
    iv_high: float | None = None
    iv_low: float | None = None

    # Trade-side classification. Premium is total dollars, volume is contracts.
    volume_bid_side: int = 0
    volume_ask_side: int = 0
    volume_mid_side: int = 0
    premium_bid_side: float = 0.0
    premium_ask_side: float = 0.0
    premium_mid_side: float = 0.0

    source: str = "unusual_whales"

    @property
    def volume(self) -> int:
        return self.volume_bid_side + self.volume_ask_side + self.volume_mid_side

    @property
    def effective_ask(self) -> float | None:
        """Mean per-share price paid by buyers lifting the offer, this minute.

        None when nothing traded at the ask — absence of aggressive buying is not
        a price of zero.
        """
        if self.volume_ask_side <= 0:
            return None
        return round(self.premium_ask_side / (self.volume_ask_side * 100.0), 4)

    @property
    def effective_bid(self) -> float | None:
        """Mean per-share price received by sellers hitting the bid, this minute."""
        if self.volume_bid_side <= 0:
            return None
        return round(self.premium_bid_side / (self.volume_bid_side * 100.0), 4)

    @property
    def effective_spread(self) -> float | None:
        """Realised round-trip cost this minute, from executions rather than
        quotes. None unless BOTH sides traded — a one-sided minute cannot price
        a spread, and half of one is not an estimate of the whole."""
        a, b = self.effective_ask, self.effective_bid
        if a is None or b is None or a < b:
            return None
        return round(a - b, 4)


class IVHistoryPoint(BaseModel):
    ts: datetime
    iv: float  # ATM / 30-day implied volatility for that day


class IVHistory(BaseModel):
    """A daily implied-volatility series used to compute IV rank / percentile."""

    symbol: str
    points: list[IVHistoryPoint] = Field(default_factory=list)
    source: str = "unknown"

    @property
    def ivs(self) -> list[float]:
        return [p.iv for p in self.points]


class IVContext(BaseModel):
    """Implied-volatility context used to judge whether IV is favorable."""

    symbol: str
    iv30: float | None = None  # 30-day ATM implied vol
    iv_rank: float | None = None  # [0, 1]
    iv_percentile: float | None = None  # [0, 1]
    hv20: float | None = None  # 20-day realized vol
    term_structure_slope: float | None = None  # front-to-back IV slope
    iv_skew: float | None = None  # OTM-put IV minus OTM-call IV (>0 = downside fear)
    # How iv_rank/iv_percentile were derived: "iv_history" (true IV rank),
    # "hv_proxy" (realized-vol proxy), or "provider" (opaque provider field).
    iv_rank_source: str | None = None
    as_of: datetime
    source: str = "unknown"

    @property
    def iv_hv_ratio(self) -> float | None:
        if self.iv30 and self.hv20 and self.hv20 > 0:
            return self.iv30 / self.hv20
        return None
