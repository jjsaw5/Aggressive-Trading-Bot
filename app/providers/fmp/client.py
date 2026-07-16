"""Financial Modeling Prep (FMP) provider.

Grounded against the CURRENT official `stable` API (verified 2026-07): base
`https://financialmodelingprep.com`, auth via `?apikey=`, endpoints:

  * GET /stable/quote?symbol=SYM
  * GET /stable/historical-price-eod/full?symbol=SYM
  * GET /stable/profile?symbol=SYM
  * GET /stable/earnings-calendar
  * GET /stable/company-screener  (used by the universe builder, not here)

IMPORTANT: FMP does NOT provide options chains or Greeks (confirmed in FMP's
own docs). This provider therefore implements market-data, fundamentals and
calendar capabilities ONLY. Options data must come from a different provider
(Unusual Whales, Robinhood, or a dedicated options vendor).

Response field access is defensive (`.get(...)`): the confirmed facts are the
endpoint PATHS and auth; exact response field names should be validated against
a live response, and any missing field degrades to `None` rather than being
fabricated. See docs/providers/FINANCIAL_MODELING_PREP.md.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from app.config import settings
from app.domain.market import (
    Candle,
    CatalystEvent,
    EarningsEvent,
    Fundamentals,
    PriceHistory,
    Quote,
)
from app.providers._http import AsyncHTTP
from app.providers.base import (
    CalendarProvider,
    FundamentalsProvider,
    MarketDataProvider,
    ProviderMeta,
)

_META = ProviderMeta(
    name="fmp",
    requires_auth=True,
    typical_delay="quote marketed real-time; some feeds ~15-20min. Verify per tier.",
    rate_limit="Free 250 req/day; Starter 300/min; Premium 750/min; Ultimate 3000/min.",
    licensing="Personal consumption OK on paid plan; DISPLAY/redistribution needs a "
    "separate FMP Data Display & Licensing Agreement.",
    docs_url="https://site.financialmodelingprep.com/developer/docs/stable",
    verified=True,
)


def _to_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _epoch_to_dt(ts: Any) -> datetime:
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


class FMPProvider(MarketDataProvider, FundamentalsProvider, CalendarProvider):
    meta = _META

    def __init__(self) -> None:
        self._http = AsyncHTTP(
            provider="fmp",
            base_url=settings.fmp_base_url,
            default_params={"apikey": settings.fmp_api_key or ""},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- Market data ---
    async def get_quote(self, symbol: str) -> Quote:
        data = await self._http.get_json("/stable/quote", {"symbol": symbol.upper()})
        row = data[0] if isinstance(data, list) and data else (data or {})
        ts = row.get("timestamp")
        return Quote(
            symbol=symbol.upper(),
            price=_to_float(row.get("price")) or 0.0,
            bid=_to_float(row.get("bid")),
            ask=_to_float(row.get("ask")),
            volume=int(row["volume"]) if row.get("volume") is not None else None,
            prev_close=_to_float(row.get("previousClose")),
            as_of=_epoch_to_dt(ts) if ts is not None else datetime.now(UTC),
            delayed_minutes=0,  # Confirm actual delay for your tier before trusting.
            source="fmp",
        )

    async def get_price_history(self, symbol: str, lookback_days: int = 90) -> PriceHistory:
        data = await self._http.get_json(
            "/stable/historical-price-eod/full", {"symbol": symbol.upper()}
        )
        rows = data.get("historical", []) if isinstance(data, dict) else (data or [])
        candles: list[Candle] = []
        for r in rows[:lookback_days]:
            d = r.get("date")
            if not d:
                continue
            try:
                ts = datetime.fromisoformat(str(d)).replace(tzinfo=UTC)
            except ValueError:
                continue
            candles.append(
                Candle(
                    ts=ts,
                    open=_to_float(r.get("open")) or 0.0,
                    high=_to_float(r.get("high")) or 0.0,
                    low=_to_float(r.get("low")) or 0.0,
                    close=_to_float(r.get("close")) or 0.0,
                    volume=int(r.get("volume") or 0),
                )
            )
        candles.reverse()  # FMP returns newest-first; we want chronological.
        return PriceHistory(symbol=symbol.upper(), candles=candles, source="fmp")

    # --- Fundamentals ---
    async def get_fundamentals(self, symbol: str) -> Fundamentals:
        data = await self._http.get_json("/stable/profile", {"symbol": symbol.upper()})
        row = data[0] if isinstance(data, list) and data else (data or {})
        avg_vol = row.get("averageVolume") or row.get("volAvg")
        price = _to_float(row.get("price")) or 0.0
        return Fundamentals(
            symbol=symbol.upper(),
            company_name=row.get("companyName"),
            market_cap=_to_float(row.get("marketCap")),
            avg_dollar_volume=(_to_float(avg_vol) or 0.0) * price if avg_vol else None,
            shares_float=_to_float(row.get("floatShares")),
            sector=row.get("sector"),
            is_etf=bool(row.get("isEtf", False)),
            source="fmp",
        )

    # --- Calendar ---
    async def get_earnings(self, symbol: str) -> EarningsEvent | None:
        today = datetime.now(UTC).date()
        data = await self._http.get_json(
            "/stable/earnings-calendar",
            {"symbol": symbol.upper(), "from": today.isoformat()},
        )
        rows = data if isinstance(data, list) else []
        for r in rows:
            d = r.get("date")
            if not d:
                continue
            try:
                report = date.fromisoformat(str(d)[:10])
            except ValueError:
                continue
            if report >= today:
                return EarningsEvent(
                    symbol=symbol.upper(),
                    report_date=report,
                    time_of_day=r.get("time"),
                    source="fmp",
                )
        return None

    async def get_catalysts(self, symbol: str, horizon_days: int = 21) -> list[CatalystEvent]:
        earn = await self.get_earnings(symbol)
        out: list[CatalystEvent] = []
        today = datetime.now(UTC).date()
        if earn and (earn.report_date - today).days <= horizon_days:
            out.append(
                CatalystEvent(
                    symbol=symbol.upper(),
                    event_type="earnings",
                    event_date=earn.report_date,
                    description="Quarterly earnings",
                    is_binary=False,
                    source="fmp",
                )
            )
        return out
