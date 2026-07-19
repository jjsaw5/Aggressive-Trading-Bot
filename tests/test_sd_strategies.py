"""Short-duration Phase 2 — strategy detectors + detection engine."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.domain.enums import Direction, DTECategory, ShortDurationRegime
from app.domain.market import Candle, PriceHistory
from app.domain.shortduration import (
    IntradayBar,
    IntradayLevels,
    NewsItem,
    ShortDurationRegimeState,
)
from app.shortduration.strategies.base import SetupContext, flow_confirms, regime_supports
from app.shortduration.strategies.catalyst_continuation import CatalystContinuation
from app.shortduration.strategies.orb import OpeningRangeBreakout
from app.shortduration.strategies.trend_continuation import TrendContinuation
from app.shortduration.strategies.vwap_continuation import VWAPTrendContinuation

_ET = ZoneInfo("America/New_York")
_NOW = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)


def _regime(allow: bool = True, regime: ShortDurationRegime = ShortDurationRegime.RANGE_BOUND):
    return ShortDurationRegimeState(regime=regime, confidence=0.5, allow_new_trades=allow, as_of=_NOW)


def _bars(prices: list[float], vols: list[float] | None = None) -> list[IntradayBar]:
    start = datetime(2026, 7, 17, 9, 30, tzinfo=_ET)
    vols = vols or [10_000.0] * len(prices)
    return [
        IntradayBar(
            ts=(start + timedelta(minutes=i)).astimezone(UTC),
            open=p, high=p + 0.2, low=p - 0.2, close=p, volume=v,
        )
        for i, (p, v) in enumerate(zip(prices, vols, strict=False))
    ]


# --- ORB ---------------------------------------------------------------------
def test_orb_bullish_fires_on_confirmed_break() -> None:
    lv = IntradayLevels(
        symbol="SPY", session_date=date(2026, 7, 17), last=101.5, vwap=100.0,
        opening_range_high=101.0, opening_range_low=99.0, relative_volume=2.0,
        computed_at=_NOW,
    )
    ctx = SetupContext(symbol="SPY", now=_NOW, regime=_regime(), levels=lv, bars_1m=_bars([101.4, 101.5]))
    det = OpeningRangeBreakout().detect(ctx)
    assert det is not None and det.direction == Direction.BULLISH
    assert det.setup_score > 0.5


def test_orb_no_fire_inside_range() -> None:
    lv = IntradayLevels(
        symbol="SPY", session_date=date(2026, 7, 17), last=100.2, vwap=100.0,
        opening_range_high=101.0, opening_range_low=99.0, relative_volume=2.0, computed_at=_NOW,
    )
    ctx = SetupContext(symbol="SPY", now=_NOW, regime=_regime(), levels=lv)
    assert OpeningRangeBreakout().detect(ctx) is None


def test_orb_no_fire_when_below_vwap() -> None:
    # Broke OR high but price is below VWAP -> not a clean bullish break.
    lv = IntradayLevels(
        symbol="SPY", session_date=date(2026, 7, 17), last=101.5, vwap=102.0,
        opening_range_high=101.0, opening_range_low=99.0, relative_volume=2.0,
        computed_at=_NOW,
    )
    ctx = SetupContext(symbol="SPY", now=_NOW, regime=_regime(), levels=lv, bars_1m=_bars([101.5]))
    assert OpeningRangeBreakout().detect(ctx) is None


# --- VWAP continuation -------------------------------------------------------
def test_vwap_continuation_bullish_on_rising_structure() -> None:
    prices = [100.5 + i * 0.05 for i in range(20)]  # steadily rising, above vwap
    lv = IntradayLevels(
        symbol="QQQ", session_date=date(2026, 7, 17), last=prices[-1], vwap=100.0,
        computed_at=_NOW,
    )
    ctx = SetupContext(
        symbol="QQQ", now=_NOW, regime=_regime(),
        levels=lv, bars_1m=_bars(prices, [10_000.0] * 13 + [20_000.0] * 7),
    )
    det = VWAPTrendContinuation().detect(ctx)
    assert det is not None and det.direction == Direction.BULLISH


def test_vwap_continuation_no_fire_when_flat() -> None:
    prices = [100.5] * 20
    lv = IntradayLevels(symbol="QQQ", session_date=date(2026, 7, 17), last=100.5, vwap=100.0, computed_at=_NOW)
    ctx = SetupContext(symbol="QQQ", now=_NOW, regime=_regime(), levels=lv, bars_1m=_bars(prices))
    assert VWAPTrendContinuation().detect(ctx) is None


# --- Trend continuation (1-5DTE) --------------------------------------------
def _daily(closes: list[float]) -> PriceHistory:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [
        Candle(ts=base + timedelta(days=i), open=c, high=c + 1, low=c - 1, close=c, volume=1_000_000)
        for i, c in enumerate(closes)
    ]
    return PriceHistory(symbol="AAPL", candles=candles, source="test")


def test_trend_continuation_fires_on_daily_uptrend() -> None:
    closes = [100 + i * 0.8 for i in range(60)]  # clean uptrend -> SMA20>SMA50
    lv = IntradayLevels(symbol="AAPL", session_date=date(2026, 7, 17), last=200, vwap=190, computed_at=_NOW)
    ctx = SetupContext(symbol="AAPL", now=_NOW, regime=_regime(), levels=lv, daily=_daily(closes))
    det = TrendContinuation().detect(ctx)
    assert det is not None and det.direction == Direction.BULLISH
    assert "SMA20" in det.invalidation


def test_trend_continuation_no_fire_when_intraday_contradicts() -> None:
    closes = [100 + i * 0.8 for i in range(60)]  # daily up
    lv = IntradayLevels(symbol="AAPL", session_date=date(2026, 7, 17), last=180, vwap=190, computed_at=_NOW)
    ctx = SetupContext(symbol="AAPL", now=_NOW, regime=_regime(), levels=lv, daily=_daily(closes))
    assert TrendContinuation().detect(ctx) is None  # below VWAP contradicts the up trend


# --- Catalyst continuation (1-5DTE) -----------------------------------------
def _news() -> list[NewsItem]:
    return [NewsItem(id="n1", symbol="AAPL", headline="Upgrade", received_ts=_NOW,
                     source_ts=_NOW - timedelta(hours=2))]


def test_catalyst_continuation_requires_price_move_not_headline_alone() -> None:
    flat = _daily([150.0] * 30)  # catalyst present but NO multi-session move
    ctx = SetupContext(symbol="AAPL", now=_NOW, regime=_regime(), daily=flat, news=_news())
    assert CatalystContinuation().detect(ctx) is None  # headline alone must not trigger


def test_catalyst_continuation_fires_with_move_and_volume() -> None:
    closes = [150.0] * 27 + [153.0, 156.0, 159.0]  # +6% over last 3 sessions
    base = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [
        Candle(ts=base + timedelta(days=i), open=c, high=c + 1, low=c - 1, close=c,
               volume=1_000_000 if i < 27 else 3_000_000)
        for i, c in enumerate(closes)
    ]
    ctx = SetupContext(
        symbol="AAPL", now=_NOW, regime=_regime(),
        daily=PriceHistory(symbol="AAPL", candles=candles, source="test"), news=_news(),
    )
    det = CatalystContinuation().detect(ctx)
    assert det is not None and det.direction == Direction.BULLISH
    assert any("Catalyst" in r for r in det.reasons)


# --- helpers -----------------------------------------------------------------
def test_regime_supports_blocks_contradiction() -> None:
    bear = _regime(regime=ShortDurationRegime.BEAR_TREND)
    assert regime_supports(bear, Direction.BULLISH) is False
    assert regime_supports(bear, Direction.BEARISH) is True


def test_flow_confirms_reads_sentiment() -> None:
    from app.domain.options import FlowAlert

    bull = [FlowAlert(symbol="X", sentiment=0.6, ts=_NOW), FlowAlert(symbol="X", sentiment=0.4, ts=_NOW)]
    assert flow_confirms(bull, Direction.BULLISH) is True
    assert flow_confirms([], Direction.BULLISH) is None


# --- engine ------------------------------------------------------------------
async def test_run_detection_persists_and_ranks() -> None:
    from app.domain.enums import CandidateState
    from app.shortduration.detection import run_detection

    cands = await run_detection(DTECategory.SHORT_DTE, now=_NOW)
    # Mock universe yields setups; each is scored and classified past DETECTED,
    # or REJECTED when no liquid defined-risk contract fits the cap (Phase 4).
    scored_states = {CandidateState.EVALUATING, CandidateState.WATCHLIST,
                     CandidateState.ARMED, CandidateState.REJECTED}
    assert all(c.state in scored_states for c in cands)
    assert cands == sorted(cands, key=lambda c: c.score, reverse=True)
    if cands:
        assert cands[0].strategy is not None and cands[0].reasons
