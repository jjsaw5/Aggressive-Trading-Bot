"""Directional-thesis builder + reversal-risk flag (deterministic, informational)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.enums import Direction, DTECategory, ShortDurationStrategy
from app.domain.market import Candle, PriceHistory
from app.shortduration.strategies.base import SetupContext, StrategyDetection
from app.shortduration.thesis import build_directional_thesis

_NOW = datetime(2026, 7, 21, 15, 0, tzinfo=UTC)


def _regime():
    from app.domain.enums import ShortDurationRegime
    from app.domain.shortduration import ShortDurationRegimeState

    return ShortDurationRegimeState(regime=ShortDurationRegime.RANGE_BOUND, confidence=0.5,
                                    allow_new_trades=True, as_of=_NOW)


def _daily(closes: list[float]) -> PriceHistory:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    return PriceHistory(
        symbol="TSLA",
        candles=[Candle(ts=base + timedelta(days=i), open=c, high=c + 2, low=c - 2, close=c,
                        volume=30_000_000) for i, c in enumerate(closes)],
        source="test",
    )


def _downtrend_closes() -> list[float]:
    # 60 sessions sliding steeply down: SMA20 ends below SMA50, and price ends ~6%
    # below SMA20 (the real TSLA shape) — far enough that proximity-to-invalidation
    # does NOT fire, so a counter-trend day is the deciding reversal-risk factor.
    return [430 - i * 2.0 for i in range(60)]


def _det(direction=Direction.BEARISH, dte=DTECategory.SHORT_DTE):
    return StrategyDetection(
        strategy=ShortDurationStrategy.TREND_CONTINUATION, dte_category=dte, direction=direction,
        setup_score=0.7, entry_trigger="e", invalidation="Daily close back above SMA20.",
    )


def _ctx(change_pct=None, daily=None, levels=None):
    return SetupContext(symbol="TSLA", now=_NOW, regime=_regime(), daily=daily,
                        levels=levels, change_pct=change_pct)


def test_bearish_thesis_reads_daily_downtrend() -> None:
    th = build_directional_thesis(_ctx(change_pct=-1.0, daily=_daily(_downtrend_closes())), _det())
    assert th.direction == Direction.BEARISH
    assert "Bearish" in th.headline and "downtrend" in th.headline
    assert any("20-day mean" in d for d in th.drivers)
    assert th.invalidation_price is not None  # SMA20 level
    assert th.distance_to_invalidation_pct is not None


def test_big_green_day_flags_reversal_risk_on_a_bearish_call() -> None:
    # The TSLA scenario: bearish daily trend, but +3.6% today -> counter-trend day.
    th = build_directional_thesis(_ctx(change_pct=3.6, daily=_daily(_downtrend_closes())), _det())
    assert th.reversal_risk in ("elevated", "high")
    assert any("against the bearish thesis" in r for r in th.reversal_risk_reasons)
    assert "bounce" in th.todays_context and "thesis is intact" in th.todays_context


def test_with_trend_day_is_low_risk() -> None:
    # A down day on a bearish trend is with-trend -> no counter-trend flag.
    th = build_directional_thesis(_ctx(change_pct=-1.5, daily=_daily(_downtrend_closes())), _det())
    assert th.reversal_risk == "low"
    assert "with the thesis" in th.todays_context


def test_counter_trend_plus_news_stacks_to_high() -> None:
    from app.shortduration.scoring.models import NewsScore

    # Counter-trend day + a material catalyst -> two factors -> HIGH.
    news = NewsScore(total=0.7, source_authority=0.9, novelty=1.0, materiality=0.8, relevance=1.0,
                     price_confirmed=0.5, volume_confirmed=0.5, flow_confirmed=0.0,
                     direction=Direction.BULLISH)
    th = build_directional_thesis(
        _ctx(change_pct=3.6, daily=_daily(_downtrend_closes())), _det(), news_score=news
    )
    assert th.reversal_risk == "high"
    assert any("news catalyst" in r for r in th.reversal_risk_reasons)


async def test_thesis_attached_to_candidates() -> None:
    from app.shortduration.detection import run_detection

    cands = await run_detection(DTECategory.SHORT_DTE, now=datetime(2026, 7, 17, 15, 0, tzinfo=UTC))
    assert cands
    assert all(c.thesis is not None and c.thesis.headline for c in cands)
    assert all(c.thesis.reversal_risk in ("low", "elevated", "high") for c in cands)
