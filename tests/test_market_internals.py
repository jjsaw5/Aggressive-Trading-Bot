"""Phase 1 — real market internals vs watchlist participation (proxy)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.internals import MarketInternals
from app.providers.internals import _composite_breadth
from app.shortduration.breadth import compute_participation
from app.shortduration.regime import compute_regime

_NOW = datetime(2026, 7, 20, 15, 0, tzinfo=UTC)


def _levels(above: dict[str, bool]):
    from app.domain.shortduration import IntradayLevels
    out = {}
    for sym, up in above.items():
        out[sym] = IntradayLevels(
            symbol=sym, session_date=_NOW.date(), last=100.0,
            vwap=99.0 if up else 101.0, computed_at=_NOW,
        )
    return out


def test_participation_is_not_labeled_market_breadth() -> None:
    wp = compute_participation(list(_levels({"SPY": True, "QQQ": False}).values()))
    assert wp.is_proxy is True
    assert "breadth" not in wp.note.lower() or "not exchange breadth" in wp.note.lower()


def test_real_internals_drive_regime_and_lift_confidence_cap() -> None:
    lv = _levels({"SPY": True, "QQQ": True, "IWM": True})
    internals = MarketInternals(as_of=_NOW, source="fmp+uw", is_authoritative=True, breadth_score=0.78)
    reg = compute_regime(
        index_change_pct={"SPY": 0.8, "QQQ": 0.7, "IWM": 0.6}, index_levels=lv,
        participation=compute_participation(list(lv.values())), internals=internals,
        vol_reading=0.3, next_event=None, now=_NOW,
    )
    assert reg.breadth_is_proxy is False
    assert reg.internals_breadth_score == 0.78
    assert reg.confidence > 0.6  # real internals lift the proxy cap


def test_proxy_only_caps_confidence_and_does_not_hard_gate() -> None:
    # A tech-heavy watchlist reads 100% "up", but as a proxy it must not gate the
    # trend nor push confidence above the cap.
    lv = _levels({"SPY": True, "QQQ": True, "IWM": True})
    reg = compute_regime(
        index_change_pct={"SPY": 0.8, "QQQ": 0.7, "IWM": 0.6}, index_levels=lv,
        participation=compute_participation(list(lv.values())), internals=None,
        vol_reading=0.3, next_event=None, now=_NOW,
    )
    assert reg.breadth_is_proxy is True
    assert reg.confidence <= 0.6
    assert reg.watchlist_participation_pct == 1.0  # proxy value recorded, but not authoritative


def test_missing_internals_and_participation_leaves_breadth_none() -> None:
    lv = _levels({})  # nothing decisive
    reg = compute_regime(
        index_change_pct={"SPY": 0.8, "QQQ": 0.7, "IWM": 0.6}, index_levels=lv,
        participation=compute_participation([]), internals=None,
        vol_reading=None, next_event=None, now=_NOW,
    )
    assert reg.breadth_above_vwap_pct is None
    assert reg.regime.value == "bull_trend"  # index votes still classify; breadth just doesn't gate


def test_conflicting_internals_vs_index_direction() -> None:
    # Indexes bullish but real internals bearish -> not a clean bull_breadth gate,
    # so a HIGH_VOL/real-breadth path won't over-confirm; regime still bull by votes,
    # but confidence reflects the weak (non-strong) breadth bonus.
    lv = _levels({"SPY": True, "QQQ": True, "IWM": True})
    internals = MarketInternals(as_of=_NOW, source="fmp+uw", is_authoritative=True, breadth_score=0.3)
    reg = compute_regime(
        index_change_pct={"SPY": 0.8, "QQQ": 0.7, "IWM": 0.6}, index_levels=lv,
        participation=compute_participation(list(lv.values())), internals=internals,
        vol_reading=0.3, next_event=None, now=_NOW,
    )
    assert reg.breadth_is_proxy is False and reg.internals_breadth_score == 0.3


def test_composite_breadth_blends_available_signals() -> None:
    mi = MarketInternals(as_of=_NOW, sector_breadth_pct=0.6, tide_direction=0.4, sector_flow_pct=0.5)
    # (0.6 + (0.4+1)/2 + 0.5) / 3 = (0.6 + 0.7 + 0.5)/3 = 0.6
    assert _composite_breadth(mi) == 0.6
    assert _composite_breadth(MarketInternals(as_of=_NOW)) is None  # nothing -> None, never faked


async def test_mock_internals_provider_is_authoritative() -> None:
    from app.providers import registry
    mi = await registry.market_internals_provider().get_market_internals()
    assert mi.is_authoritative and mi.breadth_score is not None
    assert len(mi.unavailable_fields) == 4  # A/D, TICK, up/down vol, highs/lows honestly absent
