"""Deterministic synthetic-data provider.

Implements every capability interface so the full scan -> rank -> propose ->
paper pipeline runs with no external dependencies. Data is seeded off the
symbol string so runs are reproducible (important for tests and backtests).

This is NOT random noise: values are constructed to exercise the filters and
scorers realistically (some symbols look liquid and bullish, others illiquid).
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.domain.enums import OptionType
from app.domain.market import (
    Candle,
    CatalystEvent,
    EarningsEvent,
    Fundamentals,
    PriceHistory,
    Quote,
)
from app.domain.options import (
    FlowAlert,
    Greeks,
    IVContext,
    IVHistory,
    IVHistoryPoint,
    OptionChain,
    OptionContract,
)
from app.domain.shortduration import EconomicEvent, IntradayBar, NewsItem
from app.providers.base import (
    BrokerageProvider,
    CalendarProvider,
    EconomicCalendarProvider,
    FundamentalsProvider,
    IntradayProvider,
    IVHistoryProvider,
    MarketDataProvider,
    NewsProvider,
    OptionsChainProvider,
    OptionsFlowProvider,
    ProviderMeta,
)
from app.quant.pricing import black_scholes_delta, black_scholes_price

_ET_TZ = ZoneInfo("America/New_York")

_META = ProviderMeta(
    name="mock",
    requires_auth=False,
    typical_delay="synthetic (instant)",
    rate_limit="none",
    licensing="internal test data only",
    docs_url=None,
    verified=True,
)

# Rough anchor prices so mock quotes are recognizable during manual testing.
_ANCHOR_PRICE = {
    "SPY": 560.0, "QQQ": 495.0, "IWM": 220.0, "AAPL": 225.0, "MSFT": 460.0,
    "NVDA": 128.0, "AMD": 165.0, "META": 540.0, "AMZN": 195.0, "GOOGL": 180.0,
    "TSLA": 250.0, "NFLX": 680.0,
}


def _seed(symbol: str) -> float:
    """Stable pseudo-random value in [0, 1) derived from the symbol."""
    h = hashlib.sha256(symbol.encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def _u(symbol: str, i: int, k: int) -> float:
    """Deterministic uniform in (0, 1) from (symbol, day, stream)."""
    h = hashlib.sha256(f"{symbol}|{i}|{k}".encode()).hexdigest()
    return (int(h[:8], 16) + 1) / (0xFFFFFFFF + 2)


def _norm(symbol: str, i: int) -> float:
    """Deterministic standard-normal draw via Box-Muller (reproducible)."""
    u1 = _u(symbol, i, 1)
    u2 = _u(symbol, i, 2)
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def _anchor(symbol: str) -> float:
    return _ANCHOR_PRICE.get(symbol.upper(), 50.0 + _seed(symbol) * 150.0)


class MockProvider(
    MarketDataProvider,
    FundamentalsProvider,
    OptionsChainProvider,
    OptionsFlowProvider,
    IVHistoryProvider,
    CalendarProvider,
    IntradayProvider,
    NewsProvider,
    EconomicCalendarProvider,
    BrokerageProvider,
):
    meta = _META

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime.now(UTC)

    # --- Market data ---
    async def get_quote(self, symbol: str) -> Quote:
        s = _seed(symbol)
        price = round(_anchor(symbol) * (1 + (s - 0.5) * 0.02), 2)
        prev_close = round(price / (1 + (s - 0.5) * 0.03), 2)
        spread = 0.0005 + s * 0.001  # tight for liquid names
        half = price * spread / 2
        return Quote(
            symbol=symbol.upper(),
            price=price,
            bid=round(price - half, 2),
            ask=round(price + half, 2),
            volume=int(2_000_000 + s * 40_000_000),
            prev_close=prev_close,
            as_of=self._now,
            delayed_minutes=0,
            source="mock",
        )

    async def get_price_history(self, symbol: str, lookback_days: int = 90) -> PriceHistory:
        """Deterministic seeded GBM whose realized volatility matches the
        symbol's option IV (iv = 0.20 + s*0.55), so HV, IV/HV, and the HV-proxy
        IV rank are internally coherent."""
        s = _seed(symbol)
        base = _anchor(symbol)
        iv = 0.20 + s * 0.55
        daily_vol = iv / math.sqrt(252)
        daily_drift = (s - 0.45) * 0.0006  # small directional bias for trend signal
        candles: list[Candle] = []
        # Start below/above anchor so the walk ends near the anchor price.
        price = base * math.exp(-daily_drift * lookback_days)
        for i in range(lookback_days):
            ts = self._now - timedelta(days=lookback_days - i)
            z = _norm(symbol, i)
            ret = daily_drift + daily_vol * z
            price = max(1.0, price * math.exp(ret))
            hi = price * (1 + daily_vol / 2)
            lo = price * (1 - daily_vol / 2)
            candles.append(
                Candle(
                    ts=ts,
                    open=round(price, 2),
                    high=round(hi, 2),
                    low=round(lo, 2),
                    close=round(price, 2),
                    volume=int(2_000_000 + s * 30_000_000),
                )
            )
        return PriceHistory(symbol=symbol.upper(), candles=candles, source="mock")

    # --- Fundamentals ---
    async def get_fundamentals(self, symbol: str) -> Fundamentals:
        s = _seed(symbol)
        is_etf = symbol.upper() in {"SPY", "QQQ", "IWM"}
        price = _anchor(symbol)
        return Fundamentals(
            symbol=symbol.upper(),
            company_name=f"{symbol.upper()} Mock Corp",
            market_cap=price * (1_000_000_000 + s * 2_000_000_000_000),
            avg_dollar_volume=price * (2_000_000 + s * 40_000_000),
            shares_float=500_000_000 + s * 5_000_000_000,
            sector="ETF" if is_etf else "Technology",
            is_etf=is_etf,
            source="mock",
        )

    # --- Options chain / IV ---
    def _build_contracts(
        self, symbol: str, spot: float, iv: float, exps: list[date]
    ) -> list[OptionContract]:
        """Synthetic BS-priced strike ladder for the given expirations."""
        s = _seed(symbol)
        today = self._now.date()
        step = max(0.5, round(spot * 0.005 * 2) / 2)  # ~$1 increment
        n_strikes = 24
        atm = round(spot / step) * step
        contracts: list[OptionContract] = []
        for exp in exps:
            t = max(0.0, (exp - today).days) / 365.0
            for j in range(-n_strikes, n_strikes + 1):
                strike = round(atm + j * step, 2)
                if strike <= 0:
                    continue
                moneyness = abs(strike - spot) / spot
                for otype in (OptionType.CALL, OptionType.PUT):
                    price = black_scholes_price(spot, strike, t, iv, otype)
                    if price < 0.02:
                        continue  # skip worthless far-OTM contracts
                    delta = black_scholes_delta(spot, strike, t, iv, otype)
                    spread_frac = 0.01 + moneyness * 0.08  # ATM tight, wings wide
                    half = price * spread_frac / 2
                    oi = int(max(50, 6000 * math.exp(-moneyness * 12)))
                    vol = int(oi * (0.2 + s))
                    contracts.append(
                        OptionContract(
                            symbol=symbol.upper(),
                            option_symbol=f"{symbol.upper()}{exp:%y%m%d}"
                            f"{'C' if otype == OptionType.CALL else 'P'}"
                            f"{int(strike * 1000):08d}",
                            expiration=exp,
                            strike=strike,
                            option_type=otype,
                            bid=round(max(0.01, price - half), 2),
                            ask=round(price + half, 2),
                            mark=round(price, 2),
                            last=round(price, 2),
                            volume=vol,
                            open_interest=oi,
                            implied_volatility=round(iv, 4),
                            greeks=Greeks(delta=round(delta, 3), gamma=0.01, theta=-0.03, vega=0.1),
                            as_of=self._now,
                            delayed_minutes=0,
                            source="mock",
                        )
                    )
        return contracts

    async def get_option_chain(self, symbol: str, expirations: int = 4) -> OptionChain:
        """Synthetic chain priced with the SAME Black-Scholes model the engine
        and backtester use, so entry marks, sizing, and repricing are
        internally consistent. A ~$1-wide strike ladder gives realistic,
        affordable defined-risk verticals."""
        spot = (await self.get_quote(symbol)).price
        iv = 0.20 + _seed(symbol) * 0.55
        today = self._now.date()
        exps = [today + timedelta(days=7 * w + 2) for w in range(1, expirations + 1)]
        return OptionChain(
            symbol=symbol.upper(),
            underlying_price=spot,
            contracts=self._build_contracts(symbol, spot, iv, exps),
            as_of=self._now,
            source="mock",
        )

    async def get_option_chain_for_expirations(
        self, symbol: str, expirations: list[date]
    ) -> OptionChain:
        """Build the ladder at the requested expirations (for position monitoring)."""
        spot = (await self.get_quote(symbol)).price
        iv = 0.20 + _seed(symbol) * 0.55
        return OptionChain(
            symbol=symbol.upper(),
            underlying_price=spot,
            contracts=self._build_contracts(symbol, spot, iv, list(expirations)),
            as_of=self._now,
            source="mock",
        )

    async def get_iv_context(self, symbol: str) -> IVContext:
        s = _seed(symbol)
        iv30 = 0.20 + s * 0.55
        hv20 = iv30 * (0.7 + s * 0.4)
        return IVContext(
            symbol=symbol.upper(),
            iv30=round(iv30, 4),
            iv_rank=round(s, 3),
            iv_percentile=round(min(1.0, s * 1.1), 3),
            hv20=round(hv20, 4),
            term_structure_slope=round((s - 0.5) * 0.1, 4),
            as_of=self._now,
            source="mock",
        )

    async def get_iv_history(self, symbol: str, lookback_days: int = 365) -> IVHistory:
        """Deterministic daily IV series bracketing the current iv30 so the
        computed IV rank is stable and non-degenerate for tests."""
        s = _seed(symbol)
        iv30 = 0.20 + s * 0.55
        span = 0.30
        lo = max(0.05, iv30 - s * span)
        hi = iv30 + (1 - s) * span
        points: list[IVHistoryPoint] = []
        for i in range(lookback_days):
            ts = self._now - timedelta(days=lookback_days - i)
            frac = 0.5 + 0.5 * math.sin(i / 5.0 + s * 6.28)
            iv = lo + (hi - lo) * frac
            points.append(IVHistoryPoint(ts=ts, iv=round(iv, 4)))
        return IVHistory(symbol=symbol.upper(), points=points, source="mock")

    # --- Options flow ---
    async def get_flow_alerts(
        self, symbol: str | None = None, unusual_only: bool = True, limit: int = 100
    ) -> list[FlowAlert]:
        symbols = [symbol] if symbol else list(_ANCHOR_PRICE.keys())
        alerts: list[FlowAlert] = []
        for sym in symbols:
            s = _seed(sym + "flow")
            n = int(1 + s * 4)
            spot = _anchor(sym)
            for i in range(n):
                bull = _seed(sym + str(i)) > 0.45
                alerts.append(
                    FlowAlert(
                        symbol=sym.upper(),
                        option_type=OptionType.CALL if bull else OptionType.PUT,
                        strike=round(spot * (1 + (0.02 if bull else -0.02)), 1),
                        expiration=self._now.date() + timedelta(days=14),
                        premium=round(10_000 + s * 2_000_000, 2),
                        size=int(100 + s * 4000),
                        open_interest=int(500 + s * 3000),
                        is_sweep=s > 0.6,
                        is_opening=True,
                        at_ask=bull,
                        sentiment=round((0.6 if bull else -0.6) * (0.5 + s / 2), 3),
                        ts=self._now - timedelta(minutes=i * 5),
                        source="mock",
                    )
                )
        return alerts[:limit]

    # --- Calendar ---
    async def get_earnings(self, symbol: str) -> EarningsEvent | None:
        s = _seed(symbol + "earn")
        if s < 0.4:
            return None
        return EarningsEvent(
            symbol=symbol.upper(),
            report_date=self._now.date() + timedelta(days=int(2 + s * 25)),
            time_of_day="amc" if s > 0.5 else "bmo",
            source="mock",
        )

    async def get_catalysts(self, symbol: str, horizon_days: int = 21) -> list[CatalystEvent]:
        earn = await self.get_earnings(symbol)
        out: list[CatalystEvent] = []
        if earn and (earn.report_date - self._now.date()).days <= horizon_days:
            out.append(
                CatalystEvent(
                    symbol=symbol.upper(),
                    event_type="earnings",
                    event_date=earn.report_date,
                    description="Quarterly earnings",
                    is_binary=False,
                    source="mock",
                )
            )
        return out

    # --- Brokerage ---
    async def get_account_equity(self) -> float:
        return 2_000.0

    async def get_open_option_symbols(self) -> list[str]:
        return []

    # --- Intraday / news / economic calendar (deterministic) ---
    async def get_intraday_bars(
        self, symbol: str, *, interval: str = "1min", session_date: date | None = None
    ) -> list[IntradayBar]:
        """A synthetic RTH session (09:30-16:00 ET) of seeded GBM bars, so VWAP,
        opening range, and relative volume can be computed deterministically."""
        step = 1 if interval == "1min" else 5
        d = session_date or self._now.astimezone(_ET_TZ).date()
        open_et = datetime.combine(d, time(9, 30), tzinfo=_ET_TZ)
        # Cap at "now" on the current session so bars never run into the future.
        now_et = self._now.astimezone(_ET_TZ)
        end_et = datetime.combine(d, time(16, 0), tzinfo=_ET_TZ)
        if d == now_et.date() and now_et < end_et:
            end_et = now_et
        s = _seed(symbol)
        base = _anchor(symbol)
        intraday_vol = (0.20 + s * 0.55) / math.sqrt(252) / math.sqrt(390 / step)
        drift = (s - 0.45) * 0.0002 * step
        bars: list[IntradayBar] = []
        price = base * (1 + (s - 0.5) * 0.01)
        i = 0
        t = open_et
        while t <= end_et:
            z = _norm(f"{symbol}|{d.isoformat()}", i)
            price = max(1.0, price * math.exp(drift + intraday_vol * z))
            hi = price * (1 + intraday_vol / 2)
            lo = price * (1 - intraday_vol / 2)
            vol = 30_000 + s * 120_000 + abs(z) * 20_000
            bars.append(
                IntradayBar(
                    ts=t.astimezone(UTC),
                    open=round(price, 2), high=round(hi, 2),
                    low=round(lo, 2), close=round(price, 2), volume=round(vol, 0),
                )
            )
            i += 1
            t = open_et + timedelta(minutes=step * i)
        return bars

    async def get_news(
        self,
        symbols: list[str] | None = None,
        *,
        limit: int = 50,
        since: datetime | None = None,
    ) -> list[NewsItem]:
        syms = [s.upper() for s in (symbols or ["SPY", "QQQ", "NVDA"])]
        out: list[NewsItem] = []
        for idx, sym in enumerate(syms):
            s = _seed(sym)
            pub = self._now - timedelta(minutes=2 + int(s * 20))
            out.append(
                NewsItem(
                    id=f"mock:{sym}:{idx}",
                    symbol=sym,
                    headline=f"{sym} extends move on above-average volume",
                    summary="Synthetic mock headline for local testing.",
                    source="mock-wire",
                    category="market",
                    url=f"https://example.test/{sym}/{idx}",
                    source_ts=pub,
                    provider_ts=pub + timedelta(seconds=1),
                    received_ts=self._now,
                    raw_ref=f"mock:{sym}:{idx}",
                )
            )
        if since is not None:
            out = [n for n in out if n.source_ts is None or n.source_ts >= since]
        return out[:limit]

    async def get_economic_events(
        self, *, from_date: date | None = None, to_date: date | None = None
    ) -> list[EconomicEvent]:
        # One recurring high-impact event a couple hours out, for the countdown.
        base = self._now.replace(second=0, microsecond=0) + timedelta(hours=2)
        events = [
            EconomicEvent(
                name="CPI (MoM)", category="inflation", country="US",
                scheduled_at=base, impact="high", previous=0.3, consensus=0.2,
                affected_markets=["SPY", "QQQ", "IWM"], status="scheduled", source="mock",
            ),
            EconomicEvent(
                name="Initial Jobless Claims", category="employment", country="US",
                scheduled_at=base + timedelta(days=1), impact="medium",
                previous=220000.0, consensus=225000.0, status="scheduled", source="mock",
            ),
        ]
        if from_date is not None:
            events = [e for e in events if e.scheduled_at.date() >= from_date]
        if to_date is not None:
            events = [e for e in events if e.scheduled_at.date() <= to_date]
        return events
