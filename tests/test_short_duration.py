"""Short-duration module — Phase 1: primitives, regime, providers, API."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.domain.enums import ShortDurationRegime
from app.domain.shortduration import (
    EconomicEvent,
    IntradayBar,
    IntradayLevels,
    NewsItem,
)
from app.providers.mock import MockProvider
from app.shortduration.breadth import compute_breadth, compute_participation
from app.shortduration.levels import (
    opening_range,
    relative_volume,
    vwap,
)
from app.shortduration.regime import RegimeConfig, compute_regime

_ET = ZoneInfo("America/New_York")


def _bar(session: date, hh: int, mm: int, price: float, vol: float) -> IntradayBar:
    ts = datetime(session.year, session.month, session.day, hh, mm, tzinfo=_ET).astimezone(UTC)
    return IntradayBar(ts=ts, open=price, high=price + 0.5, low=price - 0.5, close=price, volume=vol)


def _bar_at(open_dt: datetime, minutes: int, price: float, vol: float) -> IntradayBar:
    ts = (open_dt + timedelta(minutes=minutes)).astimezone(UTC)
    return IntradayBar(ts=ts, open=price, high=price + 0.5, low=price - 0.5, close=price, volume=vol)


# --- Levels ------------------------------------------------------------------
def test_vwap_is_volume_weighted() -> None:
    d = date(2026, 7, 17)
    bars = [_bar(d, 9, 30, 100.0, 100), _bar(d, 9, 31, 200.0, 300)]
    # typical=(h+l+c)/3 ≈ price; weighted avg = (100*100 + 200*300)/400 = 175
    assert vwap(bars) == 175.0


def test_opening_range_only_after_window_closes() -> None:
    d = date(2026, 7, 17)
    early = [_bar(d, 9, 30, 100, 10), _bar(d, 9, 40, 103, 10)]  # within a 15-min OR
    # Window not complete (last bar 09:40 < 09:45) -> no OR yet.
    assert opening_range(early, 15, d) == (None, None)
    full = early + [_bar(d, 9, 46, 101, 10)]
    hi, lo = opening_range(full, 15, d)
    assert hi == 103.5 and lo == 99.5  # high/low of the first-15-min bars (+/-0.5)


def test_relative_volume_uses_flat_distribution() -> None:
    d = date(2026, 7, 17)
    open_dt = datetime(2026, 7, 17, 9, 30, tzinfo=_ET)
    # 30 minutes elapsed, avg daily 3.9M -> expected = 3.9M*(30/390)=300k.
    bars = [_bar_at(open_dt, i, 100, 10_000) for i in range(31)]  # 31 bars * 10k = 310k
    rv = relative_volume(bars, d, avg_daily_volume=3_900_000)
    assert rv is not None and 0.9 < rv < 1.2


def test_relative_volume_none_without_reference() -> None:
    d = date(2026, 7, 17)
    assert relative_volume([_bar(d, 9, 30, 100, 10)], d, None) is None


def test_levels_above_vwap_property() -> None:
    lv = IntradayLevels(
        symbol="SPY", session_date=date(2026, 7, 17), last=101.0, vwap=100.0,
        opening_range_high=102.0, opening_range_low=99.0, computed_at=datetime.now(UTC),
    )
    assert lv.above_vwap is True
    assert lv.below_opening_range is False
    lv2 = IntradayLevels(symbol="X", session_date=date(2026, 7, 17), computed_at=datetime.now(UTC))
    assert lv2.above_vwap is None  # unknown, not False


# --- Breadth -----------------------------------------------------------------
def test_breadth_proxy_counts_only_decisive() -> None:
    now = datetime.now(UTC)
    d = date(2026, 7, 17)
    mk = lambda last, vw: IntradayLevels(  # noqa: E731
        symbol="X", session_date=d, last=last, vwap=vw, computed_at=now
    )
    levels = [mk(101, 100), mk(99, 100), mk(101, 100), IntradayLevels(symbol="Y", session_date=d, computed_at=now)]
    b = compute_breadth(levels)
    assert b.symbols_considered == 4
    assert b.above_vwap == 2
    assert b.above_vwap_pct == round(2 / 3, 3)  # the no-reading symbol is excluded
    assert b.is_proxy is True


# --- Regime ------------------------------------------------------------------
def _levels_for(change_map: dict[str, bool]) -> dict[str, IntradayLevels]:
    now = datetime.now(UTC)
    return {
        s: IntradayLevels(
            symbol=s, session_date=date(2026, 7, 17),
            last=101.0 if up else 99.0, vwap=100.0, computed_at=now,
        )
        for s, up in change_map.items()
    }


def test_regime_bull_trend_when_aligned() -> None:
    now = datetime.now(UTC)
    lv = _levels_for({"SPY": True, "QQQ": True, "IWM": True})
    reg = compute_regime(
        index_change_pct={"SPY": 0.8, "QQQ": 0.9, "IWM": 0.7},
        index_levels=lv,
        participation=compute_participation(list(lv.values())),
        vol_reading=0.3, next_event=None, now=now,
    )
    assert reg.regime == ShortDurationRegime.BULL_TREND
    assert reg.allow_new_trades is True
    # Proxy-only participation caps confidence at 0.6 (no real internals available).
    assert reg.confidence == 0.6 and reg.breadth_is_proxy is True


def test_regime_confidence_exceeds_cap_with_real_internals() -> None:
    from app.domain.internals import MarketInternals

    now = datetime.now(UTC)
    lv = _levels_for({"SPY": True, "QQQ": True, "IWM": True})
    internals = MarketInternals(as_of=now, source="test", is_authoritative=True, breadth_score=0.8)
    reg = compute_regime(
        index_change_pct={"SPY": 0.8, "QQQ": 0.9, "IWM": 0.7},
        index_levels=lv, participation=compute_participation(list(lv.values())),
        internals=internals, vol_reading=0.3, next_event=None, now=now,
    )
    # Real internals lift the cap and earn the full breadth bonus.
    assert reg.regime == ShortDurationRegime.BULL_TREND
    assert reg.confidence > 0.6 and reg.breadth_is_proxy is False


def test_regime_bear_trend_when_aligned_down() -> None:
    now = datetime.now(UTC)
    lv = _levels_for({"SPY": False, "QQQ": False, "IWM": False})
    reg = compute_regime(
        index_change_pct={"SPY": -0.8, "QQQ": -0.9, "IWM": -0.7},
        index_levels=lv, breadth=compute_breadth(list(lv.values())),
        vol_reading=0.3, next_event=None, now=now,
    )
    assert reg.regime == ShortDurationRegime.BEAR_TREND


def test_regime_range_bound_when_mixed() -> None:
    now = datetime.now(UTC)
    lv = _levels_for({"SPY": True, "QQQ": False, "IWM": True})
    reg = compute_regime(
        index_change_pct={"SPY": 0.5, "QQQ": -0.6, "IWM": 0.2},
        index_levels=lv, breadth=compute_breadth(list(lv.values())),
        vol_reading=0.4, next_event=None, now=now,
    )
    assert reg.regime in (ShortDurationRegime.RANGE_BOUND, ShortDurationRegime.HIGH_VOL_CHOP)


def test_regime_blocks_new_trades_in_event_blackout() -> None:
    now = datetime.now(UTC)
    event = EconomicEvent(
        name="CPI", scheduled_at=now + timedelta(minutes=5), impact="high", source="mock"
    )
    lv = _levels_for({"SPY": True, "QQQ": True, "IWM": True})
    reg = compute_regime(
        index_change_pct={"SPY": 0.8, "QQQ": 0.9, "IWM": 0.7},
        index_levels=lv, breadth=compute_breadth(list(lv.values())),
        vol_reading=0.3, next_event=event, now=now, config=RegimeConfig(),
    )
    assert reg.regime == ShortDurationRegime.MACRO_EVENT_DRIVEN
    assert reg.allow_new_trades is False
    assert reg.reduce_size is True


# --- Providers ---------------------------------------------------------------
async def test_mock_intraday_bars_are_chronological_rth() -> None:
    m = MockProvider(now=datetime(2026, 7, 17, 20, 0, tzinfo=UTC))
    bars = await m.get_intraday_bars("SPY", interval="1min")
    assert len(bars) > 300  # a full RTH session of 1-min bars
    assert bars == sorted(bars, key=lambda b: b.ts)


async def test_mock_news_has_latency_lineage() -> None:
    m = MockProvider()
    news = await m.get_news(["SPY"])
    assert news and news[0].source_ts is not None
    assert news[0].end_to_end_latency_s is not None and news[0].end_to_end_latency_s > 0


async def test_mock_econ_events_have_impact_and_countdown() -> None:
    now = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)
    m = MockProvider(now=now)
    events = await m.get_economic_events()
    assert events and events[0].impact == "high"
    assert events[0].minutes_until(now) > 0


def test_news_latency_is_a_serialized_field() -> None:
    n = NewsItem(
        id="x", headline="h", received_ts=datetime(2026, 7, 17, 12, 0, 30, tzinfo=UTC),
        source_ts=datetime(2026, 7, 17, 12, 0, 0, tzinfo=UTC),
    )
    dumped = n.model_dump()
    assert dumped["end_to_end_latency_s"] == 30.0  # computed_field is serialized


def test_fmp_datetime_parses_eastern_to_utc() -> None:
    from app.providers.fmp.client import _parse_fmp_dt

    # 09:30 ET in July (EDT, -04:00) -> 13:30 UTC.
    dt = _parse_fmp_dt("2026-07-17 09:30:00")
    assert dt is not None and dt.hour == 13 and dt.minute == 30
    assert dt.tzinfo is not None


def test_benzinga_rfc_parse_and_field_helpers() -> None:
    from app.providers.benzinga.client import _all_names, _first_name, _parse_rfc

    dt = _parse_rfc("Wed, 17 Jul 2026 16:27:41 -0400")
    assert dt is not None and dt.tzinfo is not None
    assert _first_name([{"name": "Markets"}, {"name": "Tech"}]) == "Markets"
    assert _all_names([{"name": "aapl"}, {"name": "msft"}]) == ["AAPL", "MSFT"]
    assert _first_name(None) is None and _all_names(None) == []


# --- API ---------------------------------------------------------------------
def test_short_duration_api_read_endpoints() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)

    reg = c.get("/short-duration/market-regime")
    assert reg.status_code == 200
    body = reg.json()
    assert "regime" in body["regime"] and body["breadth"]["is_proxy"] is True
    assert len(body["levels"]) >= 3

    cfg = c.get("/short-duration/configuration").json()
    assert cfg["live_trading_enabled"] is False
    assert cfg["providers"]["intraday"] == "mock"

    assert c.get("/short-duration/events").status_code == 200
    news = c.get("/short-duration/news").json()
    assert news and "end_to_end_latency_s" in news[0]


def test_short_duration_scan_and_state_machine() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    scanned = c.post("/short-duration/scans/0dte").json()
    assert scanned["created"] >= 1

    cands = c.get("/short-duration/0dte/candidates").json()
    assert cands
    cid = cands[0]["id"]
    # Candidates are scored + classified past DETECTED, and carry a scorecard.
    assert cands[0]["state"] in {"evaluating", "watchlist", "armed"}
    assert cands[0]["scorecard"] is not None

    armed = c.post(f"/short-duration/candidates/{cid}/arm").json()
    assert armed["candidate"]["state"] == "armed"
    # Transition audit trail: the DETECTED origin and the ARMED target are recorded.
    tos = [t["to_state"] for t in armed["transitions"]]
    assert "detected" in tos and "armed" in tos

    bad = c.post(f"/short-duration/candidates/{cid}/frobnicate")
    assert bad.status_code == 400  # unknown action rejected


def test_short_duration_options_and_flow_endpoints() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    assert c.get("/short-duration/options/SPY").status_code == 200
    assert c.get("/short-duration/flow/SPY").status_code == 200
    assert c.get("/short-duration/levels/SPY").status_code == 200
