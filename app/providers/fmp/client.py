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
from zoneinfo import ZoneInfo

from app.config import settings
from app.domain.market import (
    Candle,
    CatalystEvent,
    EarningsEvent,
    Fundamentals,
    PriceHistory,
    Quote,
)
from app.domain.shortduration import EconomicEvent, IntradayBar, NewsItem
from app.providers._http import AsyncHTTP
from app.providers.base import (
    CalendarProvider,
    EconomicCalendarProvider,
    FundamentalsProvider,
    IntradayProvider,
    MarketDataProvider,
    NewsProvider,
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


def _epoch_to_dt(ts: Any) -> datetime | None:
    # None on an unparseable epoch — never substitute "now", which would disguise
    # a quote of unknown age as seconds-fresh and slip it past the freshness gate.
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC)
    except (TypeError, ValueError):
        return None


# FMP intraday/news timestamps are US/Eastern wall-clock without a tzoffset
# (e.g. "2026-07-17 15:59:00"). Localize to ET, then normalize to UTC so the
# whole platform stays UTC-internal.
_ET = ZoneInfo("America/New_York")


def _parse_fmp_dt(value: Any) -> datetime | None:
    """Parse an FMP datetime string. Accepts ISO with or without a tz; naive
    values are assumed US/Eastern (FMP's convention) and converted to UTC."""
    if value is None:
        return None
    s = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return dt.replace(tzinfo=_ET).astimezone(UTC)
    try:  # explicit offset, e.g. "...+00:00"
        dt = datetime.fromisoformat(str(value))
        return (dt if dt.tzinfo else dt.replace(tzinfo=_ET)).astimezone(UTC)
    except ValueError:
        return None


class FMPProvider(
    MarketDataProvider,
    FundamentalsProvider,
    CalendarProvider,
    IntradayProvider,
    NewsProvider,
    EconomicCalendarProvider,
):
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
            # No timestamp -> as_of=None (unknown age), NOT now(); the freshness gate
            # treats that as stale rather than trusting fabricated freshness.
            as_of=_epoch_to_dt(ts),
            delayed_minutes=settings.fmp_quote_delay_minutes,
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

    # --- Intraday bars (grounded on /stable/historical-chart/{interval}; field
    # mapping validated defensively — see module note and docs/SHORT_DURATION.md) ---
    async def get_intraday_bars(
        self, symbol: str, *, interval: str = "1min", session_date: date | None = None,
        from_date: date | None = None, to_date: date | None = None,
    ) -> list[IntradayBar]:
        if interval not in ("1min", "5min"):
            raise ValueError("interval must be '1min' or '5min'")
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if session_date is not None:
            params["from"] = session_date.isoformat()
            params["to"] = session_date.isoformat()
        elif from_date is not None or to_date is not None:
            if from_date is not None:
                params["from"] = from_date.isoformat()
            if to_date is not None:
                params["to"] = to_date.isoformat()
        data = await self._http.get_json(f"/stable/historical-chart/{interval}", params)
        rows = data if isinstance(data, list) else []
        bars: list[IntradayBar] = []
        for r in rows:
            ts = _parse_fmp_dt(r.get("date"))
            if ts is None:
                continue
            bars.append(
                IntradayBar(
                    ts=ts,
                    open=_to_float(r.get("open")) or 0.0,
                    high=_to_float(r.get("high")) or 0.0,
                    low=_to_float(r.get("low")) or 0.0,
                    close=_to_float(r.get("close")) or 0.0,
                    volume=_to_float(r.get("volume")) or 0.0,
                )
            )
        bars.sort(key=lambda b: b.ts)  # FMP returns newest-first; want chronological.
        return bars

    # --- News (grounded on /stable/stock-news) ---
    async def get_news(
        self,
        symbols: list[str] | None = None,
        *,
        limit: int = 50,
        since: datetime | None = None,
    ) -> list[NewsItem]:
        params: dict[str, Any] = {"limit": limit}
        if symbols:
            params["symbols"] = ",".join(s.upper() for s in symbols)
        data = await self._http.get_json("/stable/stock-news", params)
        rows = data if isinstance(data, list) else []
        now = datetime.now(UTC)
        out: list[NewsItem] = []
        for r in rows:
            pub = _parse_fmp_dt(r.get("publishedDate"))
            if since is not None and pub is not None and pub < since:
                continue
            title = r.get("title") or ""
            if not title:
                continue
            sym = r.get("symbol")
            out.append(
                NewsItem(
                    id=f"fmp:{r.get('url') or title}"[:180],
                    symbol=str(sym).upper() if sym else None,
                    headline=title,
                    summary=(r.get("text") or "")[:2000],
                    source=r.get("site") or r.get("publisher") or "fmp",
                    url=r.get("url"),
                    source_ts=pub,
                    provider_ts=pub,
                    received_ts=now,
                    raw_ref=r.get("url"),
                )
            )
        return out

    # --- Economic calendar (grounded on /stable/economic-calendar) ---
    async def get_economic_events(
        self, *, from_date: date | None = None, to_date: date | None = None
    ) -> list[EconomicEvent]:
        params: dict[str, Any] = {}
        if from_date is not None:
            params["from"] = from_date.isoformat()
        if to_date is not None:
            params["to"] = to_date.isoformat()
        data = await self._http.get_json("/stable/economic-calendar", params)
        rows = data if isinstance(data, list) else []
        out: list[EconomicEvent] = []
        for r in rows:
            when = _parse_fmp_dt(r.get("date"))
            name = r.get("event")
            if when is None or not name:
                continue
            impact = r.get("impact")
            out.append(
                EconomicEvent(
                    name=str(name),
                    country=r.get("country"),
                    scheduled_at=when,
                    impact=str(impact).lower() if impact else None,
                    previous=_to_float(r.get("previous")),
                    consensus=_to_float(r.get("estimate")),
                    actual=_to_float(r.get("actual")),
                    status="released" if r.get("actual") not in (None, "") else "scheduled",
                    source="fmp",
                )
            )
        out.sort(key=lambda e: e.scheduled_at)
        return out

    # --- Sector breadth (grounded on /stable/sector-performance-snapshot) ---
    async def get_sector_breadth(self, *, as_of: date | None = None) -> dict[str, float | int]:
        """Real sector breadth: how many of the 11 sectors are advancing today, and
        the average sector move. Used by the market-internals composite."""
        params: dict[str, Any] = {}
        if as_of is not None:
            params["date"] = as_of.isoformat()
        data = await self._http.get_json("/stable/sector-performance-snapshot", params)
        rows = data if isinstance(data, list) else []
        changes = [_to_float(r.get("averageChange")) for r in rows]
        changes = [c for c in changes if c is not None]
        if not changes:
            return {}
        advancing = sum(1 for c in changes if c > 0)
        return {
            "sectors_total": len(changes),
            "sectors_advancing": advancing,
            "avg_sector_change_pct": round(sum(changes) / len(changes), 4),
        }
