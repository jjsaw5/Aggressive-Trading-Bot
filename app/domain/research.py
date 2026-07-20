"""On-demand single-symbol research report.

Aggregates everything the platform already pulls for one ticker — quote, intraday
levels, flow, IV, news, catalysts, fundamentals — plus suggested plays from both
the short-duration and core engines. Every section is best-effort: a provider miss
is recorded in `errors` rather than failing the whole report.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.candidates import TradeCandidate
from app.domain.market import CatalystEvent, EarningsEvent, Fundamentals, Quote
from app.domain.options import FlowAlert, IVContext
from app.domain.shortduration import IntradayLevels, NewsItem, ShortDurationCandidate


class FlowSummary(BaseModel):
    """A quick directional read of the symbol's unusual options flow."""

    alerts: int = 0
    calls: int = 0
    puts: int = 0
    total_premium_usd: float = 0.0
    net_sentiment: float | None = None  # mean of per-alert sentiment, [-1, 1]
    top: list[FlowAlert] = Field(default_factory=list)


class SymbolReport(BaseModel):
    symbol: str
    as_of: datetime
    # Market context
    quote: Quote | None = None
    change_pct: float | None = None
    levels: IntradayLevels | None = None
    iv: IVContext | None = None
    fundamentals: Fundamentals | None = None
    # Activity
    flow: FlowSummary = Field(default_factory=FlowSummary)
    news: list[NewsItem] = Field(default_factory=list)
    earnings: EarningsEvent | None = None
    catalysts: list[CatalystEvent] = Field(default_factory=list)
    # Suggested plays (setup-first — engines produce these, not the news)
    zero_dte: list[ShortDurationCandidate] = Field(default_factory=list)
    one_five_dte: list[ShortDurationCandidate] = Field(default_factory=list)
    swing: list[TradeCandidate] = Field(default_factory=list)
    # Per-section failures (provider miss / bad symbol), never fatal
    errors: dict[str, str] = Field(default_factory=dict)
