"""Robinhood response mapping — pure, no library/network/auth required.

Sample dicts mirror robin_stocks 3.4.0 return shapes (numeric fields as strings).
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.enums import OptionType
from app.providers.base import (
    BrokerageProvider,
    MarketDataProvider,
    OptionsChainProvider,
)
from app.providers.robinhood.client import RobinhoodProvider, _span_for
from app.providers.robinhood.mapping import (
    parse_candles,
    parse_option_contract,
    parse_quote,
)

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def test_parse_quote_casts_strings() -> None:
    row = {
        "ask_price": "105.50", "bid_price": "105.40", "last_trade_price": "105.45",
        "previous_close": "104.00", "symbol": "AAA",
    }
    q = parse_quote(row, "aaa", NOW)
    assert q.symbol == "AAA"
    assert q.price == 105.45
    assert q.bid == 105.40 and q.ask == 105.50
    assert q.prev_close == 104.00
    assert q.source == "robinhood"


def test_parse_candles_skips_incomplete_and_casts() -> None:
    rows = [
        {"begins_at": "2026-05-01T00:00:00Z", "open_price": "100", "close_price": "101",
         "high_price": "102", "low_price": "99", "volume": "1000000"},
        {"begins_at": "2026-05-02T00:00:00Z", "open_price": "101"},  # no close -> skipped
    ]
    candles = parse_candles(rows)
    assert len(candles) == 1
    assert candles[0].close == 101.0 and candles[0].volume == 1_000_000


def test_parse_option_contract_call_with_greeks() -> None:
    row = {
        "strike_price": "100.0000", "type": "call", "expiration_date": "2026-07-01",
        "bid_price": "3.10", "ask_price": "3.20", "mark_price": "3.15",
        "open_interest": "1500", "volume": "200", "implied_volatility": "0.2850",
        "delta": "0.52", "gamma": "0.02", "theta": "-0.03", "vega": "0.10", "rho": "0.01",
        "occ_symbol": "AAA260701C00100000", "id": "abc",
    }
    c = parse_option_contract(row, "AAA", NOW)
    assert c is not None
    assert c.option_type == OptionType.CALL
    assert c.strike == 100.0
    assert c.mid == 3.15
    assert c.open_interest == 1500 and c.volume == 200
    assert c.implied_volatility == 0.285
    assert c.greeks.delta == 0.52
    assert c.option_symbol == "AAA260701C00100000"


def test_parse_option_contract_rejects_unknown_type() -> None:
    row = {"strike_price": "100", "type": "", "expiration_date": "2026-07-01"}
    assert parse_option_contract(row, "AAA", NOW) is None


def test_parse_option_contract_rejects_missing_strike() -> None:
    row = {"type": "put", "expiration_date": "2026-07-01"}
    assert parse_option_contract(row, "AAA", NOW) is None


def test_span_mapping() -> None:
    assert _span_for(5) == "week"
    assert _span_for(30) == "month"
    assert _span_for(90) == "3month"
    assert _span_for(252) == "year"
    assert _span_for(1000) == "5year"


def test_provider_implements_capability_interfaces() -> None:
    # Constructing the provider must not require robin_stocks (lazy import).
    p = RobinhoodProvider()
    assert isinstance(p, MarketDataProvider)
    assert isinstance(p, OptionsChainProvider)
    assert isinstance(p, BrokerageProvider)
    assert p.meta.name == "robinhood"
    assert p.meta.verified is False  # unofficial API, not live-tested
