"""Liquidity gates must reject illiquid / unreliable contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.domain.enums import OptionType
from app.domain.options import OptionContract
from app.engine.liquidity import OptionLiquidityConfig, gate_option


def _contract(**kw) -> OptionContract:
    base = {
        "symbol": "AAA",
        "expiration": date(2026, 8, 21),
        "strike": 100.0,
        "option_type": OptionType.CALL,
        "bid": 1.00,
        "ask": 1.04,
        "volume": 500,
        "open_interest": 2000,
        "as_of": datetime.now(UTC),
    }
    base.update(kw)
    return OptionContract(**base)


def test_liquid_contract_passes() -> None:
    assert gate_option(_contract(), OptionLiquidityConfig()) == []


def test_wide_spread_rejected() -> None:
    c = _contract(bid=1.00, ask=1.50)  # 40% spread
    reasons = gate_option(c, OptionLiquidityConfig())
    assert any(r.value == "wide_spread" for r in reasons)


def test_low_open_interest_rejected() -> None:
    c = _contract(open_interest=10)
    reasons = gate_option(c, OptionLiquidityConfig())
    assert any(r.value == "low_open_interest" for r in reasons)


def test_missing_price_is_unreliable() -> None:
    c = _contract(bid=None, ask=None, mark=None, last=None)
    reasons = gate_option(c, OptionLiquidityConfig())
    assert any(r.value == "unreliable_pricing" for r in reasons)


def test_too_expensive_contract_rejected_for_small_account() -> None:
    c = _contract(bid=30.0, ask=30.2)  # mid > max_mid_price
    reasons = gate_option(c, OptionLiquidityConfig())
    assert any(r.value == "unreliable_pricing" for r in reasons)
