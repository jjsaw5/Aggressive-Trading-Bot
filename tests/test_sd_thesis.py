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


def _ctx(change_pct=None, daily=None, levels=None, next_earnings=None):
    return SetupContext(symbol="TSLA", now=_NOW, regime=_regime(), daily=daily,
                        levels=levels, change_pct=change_pct, next_earnings=next_earnings)


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


def test_horizon_mismatch_flags_swing_thesis_in_short_expiry() -> None:
    # A daily-trend (swing) thesis in the 1-5DTE track can't express its horizon.
    th = build_directional_thesis(_ctx(change_pct=-1.0, daily=_daily(_downtrend_closes())), _det())
    assert any("Horizon mismatch" in w for w in th.structural_warnings)
    assert any("20–45 DTE" in w for w in th.structural_warnings)


def test_earnings_before_expiry_flagged() -> None:
    # Earnings 3 days out, inside the 1-5DTE expiry window -> event-binary warning.
    from datetime import timedelta

    th = build_directional_thesis(
        _ctx(change_pct=-1.0, daily=_daily(_downtrend_closes()),
             next_earnings=(_NOW.date() + timedelta(days=3))),
        _det(),
    )
    assert any("Earnings" in w and "event binary" in w for w in th.structural_warnings)


def test_the_tsla_case_flags_both() -> None:
    # The exact lesson: a daily-trend bearish call, expressed short, straddling earnings.
    from datetime import timedelta

    th = build_directional_thesis(
        _ctx(change_pct=3.6, daily=_daily(_downtrend_closes()),
             next_earnings=(_NOW.date() + timedelta(days=1))),
        _det(),
    )
    assert len(th.structural_warnings) == 2  # horizon + earnings
    assert th.reversal_risk in ("elevated", "high")  # the bounce is still flagged too


def test_no_structural_warning_for_intraday_setup() -> None:
    # A 0DTE VWAP setup with no near earnings is the right instrument for its thesis.
    det = StrategyDetection(
        strategy=ShortDurationStrategy.VWAP_TREND_CONTINUATION, dte_category=DTECategory.ZERO_DTE,
        direction=Direction.BULLISH, setup_score=0.6, entry_trigger="e", invalidation="lose VWAP",
    )
    th = build_directional_thesis(_ctx(change_pct=0.5), det)
    assert th.structural_warnings == []


async def test_thesis_attached_to_candidates() -> None:
    from app.shortduration.detection import run_detection

    cands = await run_detection(DTECategory.SHORT_DTE, now=datetime(2026, 7, 17, 15, 0, tzinfo=UTC))
    assert cands
    assert all(c.thesis is not None and c.thesis.headline for c in cands)
    assert all(c.thesis.reversal_risk in ("low", "elevated", "high") for c in cands)


# --- Source joins (FMP / UW / Benzinga): every claim carries its receipt ------
def test_sourced_claims_join_all_three_providers() -> None:
    from datetime import date

    from app.domain.options import FlowAlert
    from app.domain.shortduration import NewsItem

    ctx = _ctx(change_pct=-2.0, daily=_daily(_downtrend_closes()),
               next_earnings=date(2026, 7, 29))
    ctx.flow = [
        FlowAlert(symbol="TSLA", ts=_NOW, premium=500_000.0, sentiment=-0.6),
        FlowAlert(symbol="TSLA", ts=_NOW, premium=250_000.0, sentiment=0.4),
    ]
    ctx.news = [NewsItem(id="n1", symbol="TSLA", headline="TSLA misses on Q2 EPS",
                         received_ts=_NOW, source_ts=_NOW)]
    th = build_directional_thesis(ctx, _det())
    by_source = {}
    for c in th.claims:
        by_source.setdefault(c.source, []).append(c)
    assert set(th.sources_used) == {"fmp", "unusual_whales", "benzinga"}
    assert th.sources_missing == []
    # FMP: trend + move + earnings, each with the raw value attached.
    fields = {c.field for c in by_source["fmp"]}
    assert {"daily.sma20_vs_sma50", "quote.change_pct", "calendar.next_earnings"} <= fields
    # UW flow claim is observational and says so — the registry verdict travels with it.
    flow_claim = by_source["unusual_whales"][0]
    assert "predicts nothing" in flow_claim.text and "n=2" in flow_claim.value
    # Benzinga claim quotes the actual headline with its timestamp.
    news_claim = by_source["benzinga"][0]
    assert "TSLA misses" in news_claim.text and news_claim.as_of is not None


def test_silent_sources_are_named_not_papered_over() -> None:
    # Daily history only (FMP) — UW and Benzinga contributed nothing and the
    # thesis must say so rather than fabricate claims.
    th = build_directional_thesis(_ctx(daily=_daily(_downtrend_closes())), _det())
    assert th.sources_used == ["fmp"]
    assert set(th.sources_missing) == {"unusual_whales", "benzinga"}
    assert all(c.source != "unusual_whales" for c in th.claims)


def test_no_data_yields_no_claims_and_all_sources_missing() -> None:
    th = build_directional_thesis(_ctx(), _det())
    assert th.claims == [] or all(c.source == "computed" for c in th.claims)
    assert set(th.sources_missing) == {"fmp", "unusual_whales", "benzinga"}
