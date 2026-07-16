"""OCC option-symbol parsing for the UW chain provider (pure, no network)."""

from __future__ import annotations

from datetime import date

from app.domain.enums import OptionType
from app.providers.base import OptionsChainProvider
from app.providers.unusual_whales.client import UnusualWhalesProvider, parse_occ_symbol


def test_parse_occ_call() -> None:
    assert parse_occ_symbol("AAPL260717C00335000") == (
        date(2026, 7, 17), 335.0, OptionType.CALL
    )


def test_parse_occ_put_and_fractional_strike() -> None:
    # SPY 2026-08-21 put, strike 533.5
    assert parse_occ_symbol("SPY260821P00533500") == (
        date(2026, 8, 21), 533.5, OptionType.PUT
    )


def test_parse_occ_variable_length_root() -> None:
    # Single-letter root still parses from the right.
    exp, strike, ot = parse_occ_symbol("F261218C00012000")
    assert (exp, strike, ot) == (date(2026, 12, 18), 12.0, OptionType.CALL)


def test_parse_occ_rejects_garbage() -> None:
    assert parse_occ_symbol("") is None
    assert parse_occ_symbol("NOTASYMBOL") is None
    assert parse_occ_symbol("AAPL260717X00335000") is None  # bad type char


def test_uw_implements_chain_interface() -> None:
    assert issubclass(UnusualWhalesProvider, OptionsChainProvider)
