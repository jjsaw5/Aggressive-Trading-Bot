"""Phase 3.2: a strike-specific options-flow alert biases contract selection."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.domain.enums import Direction, OptionType
from app.domain.options import FlowAlert, Greeks, OptionChain, OptionContract
from app.engine.contract_selection import SelectionConfig, select_long_contract
from app.shortduration.strategies.base import dominant_flow_strike

_NOW = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)
_EXP = date(2026, 7, 22)
_ASOF = date(2026, 7, 17)


def _call(strike: float) -> OptionContract:
    # Two equally-liquid, equal-delta calls -> only the flow strike breaks the tie.
    return OptionContract(symbol="AAA", expiration=_EXP, strike=strike, option_type=OptionType.CALL,
                          bid=2.0, ask=2.05, mark=2.025, open_interest=5000, volume=800,
                          implied_volatility=0.4, greeks=Greeks(delta=0.5), as_of=_NOW, source="test")


def _chain() -> OptionChain:
    return OptionChain(symbol="AAA", underlying_price=100.0, as_of=_NOW, source="test",
                       contracts=[_call(100.0), _call(102.0)])


def _sel() -> SelectionConfig:
    return SelectionConfig(min_dte=1, max_dte=10, target_delta=0.5, min_delta=0.35, max_delta=0.65)


def test_flow_strike_shifts_selection() -> None:
    base = select_long_contract(_chain(), Direction.BULLISH, _ASOF, _sel())
    biased = select_long_contract(_chain(), Direction.BULLISH, _ASOF, _sel(), prefer_strike=102.0)
    assert base is not None and biased is not None
    # With a real flow alert at 102, selection moves off the default tie-break strike.
    assert biased.contract.strike == 102.0
    assert biased.contract.strike != base.contract.strike


def test_no_flow_leaves_selection_unbiased() -> None:
    a = select_long_contract(_chain(), Direction.BULLISH, _ASOF, _sel())
    b = select_long_contract(_chain(), Direction.BULLISH, _ASOF, _sel(), prefer_strike=None)
    assert a.contract.strike == b.contract.strike  # None -> original behaviour


def _alert(otype: OptionType, strike: float, premium: float) -> FlowAlert:
    return FlowAlert(symbol="AAA", option_type=otype, strike=strike, expiration=_EXP,
                     premium=premium, ts=_NOW, source="test")


def test_dominant_flow_strike_picks_heaviest_same_direction() -> None:
    flow = [
        _alert(OptionType.CALL, 100.0, 50_000),
        _alert(OptionType.CALL, 105.0, 250_000),  # heaviest call premium
        _alert(OptionType.PUT, 95.0, 400_000),     # ignored for a bullish thesis
    ]
    assert dominant_flow_strike(flow, Direction.BULLISH) == 105.0
    assert dominant_flow_strike(flow, Direction.BEARISH) == 95.0
    assert dominant_flow_strike([], Direction.BULLISH) is None
