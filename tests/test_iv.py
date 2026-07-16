"""IV rank/percentile computation and the IV-context builder."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.domain.enums import OptionType
from app.domain.market import Candle, PriceHistory
from app.domain.options import IVHistory, IVHistoryPoint, OptionChain, OptionContract
from app.engine.iv_context import build_iv_context
from app.quant.iv import (
    atm_iv_from_chain,
    iv_percentile,
    iv_rank,
    realized_vol,
    realized_vol_series,
)


def test_iv_rank_endpoints() -> None:
    hist = [0.2, 0.3, 0.4, 0.5, 0.6]
    assert iv_rank(0.6, hist) == 1.0
    assert iv_rank(0.2, hist) == 0.0
    assert iv_rank(0.4, hist) == 0.5


def test_iv_rank_clamps_outside_range() -> None:
    assert iv_rank(0.9, [0.2, 0.4]) == 1.0
    assert iv_rank(0.1, [0.2, 0.4]) == 0.0


def test_iv_rank_flat_history_is_mid() -> None:
    assert iv_rank(0.3, [0.3, 0.3, 0.3]) == 0.5


def test_iv_percentile() -> None:
    hist = [0.1, 0.2, 0.3, 0.4]
    assert iv_percentile(0.25, hist) == 0.5  # two of four <= 0.25
    assert iv_percentile(0.4, hist) == 1.0
    assert iv_percentile(0.05, hist) == 0.0


def test_iv_rank_empty_history_is_none() -> None:
    assert iv_rank(0.3, []) is None
    assert iv_percentile(0.3, []) is None


def test_realized_vol_series_length() -> None:
    closes = [100.0 * (1.005 ** i) for i in range(60)]
    series = realized_vol_series(closes, window=20)
    assert len(series) == 60 - 20
    assert all(v >= 0 for v in series)


def _history(n: int, vol_daily: float) -> PriceHistory:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    closes = []
    p = 100.0
    for i in range(n):
        p *= 1.0 + (vol_daily if i % 2 else -vol_daily)
        closes.append(p)
    candles = [
        Candle(ts=base + timedelta(days=i), open=c, high=c, low=c, close=c, volume=1)
        for i, c in enumerate(closes)
    ]
    return PriceHistory(symbol="AAA", candles=candles, source="test")


def test_builder_uses_iv_history_when_present() -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    pts = [IVHistoryPoint(ts=now - timedelta(days=i), iv=0.2 + 0.002 * i) for i in range(60)]
    ivh = IVHistory(symbol="AAA", points=pts, source="test")
    ctx = build_iv_context("AAA", current_iv=0.30, as_of=now, iv_history=ivh)
    assert ctx.iv_rank_source == "iv_history"
    assert ctx.iv_rank is not None
    assert ctx.iv30 == 0.30


def test_builder_falls_back_to_hv_proxy() -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    hist = _history(80, 0.01)
    ctx = build_iv_context("AAA", current_iv=0.30, as_of=now, price_history=hist)
    assert ctx.iv_rank_source == "hv_proxy"
    assert ctx.hv20 is not None


def test_builder_no_data_leaves_rank_unknown() -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    ctx = build_iv_context("AAA", current_iv=0.30, as_of=now)
    assert ctx.iv_rank is None
    assert ctx.iv_rank_source is None


def test_realized_vol_none_when_insufficient() -> None:
    assert realized_vol([100.0, 101.0], 20) is None


def test_atm_iv_from_chain_averages_near_money() -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    exp = date(2026, 7, 1)  # ~30 DTE
    contracts = [
        OptionContract(
            symbol="AAA", expiration=exp, strike=k, option_type=ot,
            implied_volatility=0.40, as_of=now,
        )
        for k in (95.0, 100.0, 105.0)
        for ot in (OptionType.CALL, OptionType.PUT)
    ]
    chain = OptionChain(symbol="AAA", underlying_price=100.0, contracts=contracts, as_of=now)
    assert atm_iv_from_chain(chain, dte_target=30) == 0.40


def test_atm_iv_from_chain_empty_is_none() -> None:
    chain = OptionChain(symbol="AAA", underlying_price=100.0, contracts=[],
                        as_of=datetime(2026, 6, 1, tzinfo=UTC))
    assert atm_iv_from_chain(chain) is None
