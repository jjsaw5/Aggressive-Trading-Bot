"""Market-data domain models (equities/ETFs)."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class Quote(BaseModel):
    """A point-in-time equity quote. `as_of` and `delayed_minutes` make the
    freshness of the data explicit — never assume real-time."""

    symbol: str
    price: float
    bid: float | None = None
    ask: float | None = None
    volume: int | None = None
    prev_close: float | None = None
    as_of: datetime
    delayed_minutes: int = 0
    source: str = "unknown"

    @property
    def change_pct(self) -> float | None:
        if self.prev_close and self.prev_close > 0:
            return (self.price - self.prev_close) / self.prev_close
        return None

    @property
    def spread_pct(self) -> float | None:
        if self.bid and self.ask and self.ask > 0:
            mid = (self.bid + self.ask) / 2
            if mid > 0:
                return (self.ask - self.bid) / mid
        return None


class Candle(BaseModel):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class PriceHistory(BaseModel):
    symbol: str
    candles: list[Candle] = Field(default_factory=list)
    source: str = "unknown"

    @property
    def closes(self) -> list[float]:
        return [c.close for c in self.candles]


class Fundamentals(BaseModel):
    symbol: str
    company_name: str | None = None
    market_cap: float | None = None
    avg_dollar_volume: float | None = None
    shares_float: float | None = None
    sector: str | None = None
    is_etf: bool = False
    source: str = "unknown"


class EarningsEvent(BaseModel):
    symbol: str
    report_date: date
    time_of_day: str | None = None  # "bmo" | "amc" | None
    source: str = "unknown"


class CatalystEvent(BaseModel):
    symbol: str
    event_type: str  # "earnings" | "dividend" | "fda" | "econ" | "split" ...
    event_date: date
    description: str | None = None
    is_binary: bool = False
    source: str = "unknown"
