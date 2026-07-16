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
from datetime import UTC, datetime, timedelta

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
    OptionChain,
    OptionContract,
)
from app.providers.base import (
    BrokerageProvider,
    CalendarProvider,
    FundamentalsProvider,
    MarketDataProvider,
    OptionsChainProvider,
    OptionsFlowProvider,
    ProviderMeta,
)
from app.quant.pricing import black_scholes_delta, black_scholes_price

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


def _anchor(symbol: str) -> float:
    return _ANCHOR_PRICE.get(symbol.upper(), 50.0 + _seed(symbol) * 150.0)


class MockProvider(
    MarketDataProvider,
    FundamentalsProvider,
    OptionsChainProvider,
    OptionsFlowProvider,
    CalendarProvider,
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
        s = _seed(symbol)
        base = _anchor(symbol)
        drift = (s - 0.45) * 0.004  # slight up/down bias per symbol
        vol = 0.008 + s * 0.02
        candles: list[Candle] = []
        price = base * (1 - drift * lookback_days)
        for i in range(lookback_days):
            ts = self._now - timedelta(days=lookback_days - i)
            wobble = math.sin(i / 3.0 + s * 6.28) * vol
            price = max(1.0, price * (1 + drift + wobble * 0.3))
            hi = price * (1 + vol / 2)
            lo = price * (1 - vol / 2)
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
    async def get_option_chain(self, symbol: str, expirations: int = 4) -> OptionChain:
        """Synthetic chain priced with the SAME Black-Scholes model the engine
        and backtester use, so entry marks, sizing, and repricing are
        internally consistent. A ~$1-wide strike ladder gives realistic,
        affordable defined-risk verticals."""
        s = _seed(symbol)
        spot = (await self.get_quote(symbol)).price
        iv = 0.20 + s * 0.55
        contracts: list[OptionContract] = []
        today = self._now.date()

        # ~$1 strike increment (rounded), a realistic ladder for these names.
        step = max(0.5, round(spot * 0.005 * 2) / 2)
        n_strikes = 24
        for w in range(1, expirations + 1):
            exp = today + timedelta(days=7 * w + 2)
            dte = (exp - today).days
            t = dte / 365.0
            atm = round(spot / step) * step
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
        return OptionChain(
            symbol=symbol.upper(),
            underlying_price=spot,
            contracts=contracts,
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
