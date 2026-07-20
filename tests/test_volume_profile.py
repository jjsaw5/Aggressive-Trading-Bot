"""Phase 1 — historical time-of-day intraday volume profile."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.domain.shortduration import IntradayBar
from app.shortduration import volume_profile as vp
from app.shortduration.volume_profile import (
    IntradayVolumeProfile,
    relative_volume,
)

_ET = ZoneInfo("America/New_York")


def _prof(minutes: dict[int, float], sessions: int) -> IntradayVolumeProfile:
    return IntradayVolumeProfile(
        symbol="SPY", as_of_date=datetime(2026, 7, 20).date(), sessions_used=sessions,
        minute_median=minutes, minute_samples=dict.fromkeys(minutes, sessions),
        is_estimated=sessions < 10, data_quality=min(1.0, sessions / 20), updated_at=datetime.now(UTC),
    )


# A realistic U-shaped cumulative baseline: fast at open, steady midday, fast at close.
_BASELINE = {0: 100_000, 30: 6_000_000, 195: 20_000_000, 360: 30_000_000, 389: 40_000_000}


def test_market_open_below_baseline() -> None:
    r = relative_volume(_prof(_BASELINE, 20), actual_cumulative=60_000, minute_of_session=0)
    assert r.method == "profile" and r.value == 0.6 and not r.is_estimated


def test_midday_at_baseline() -> None:
    r = relative_volume(_prof(_BASELINE, 20), actual_cumulative=20_000_000, minute_of_session=195)
    assert r.value == 1.0 and r.method == "profile"


def test_power_hour_elevated() -> None:
    r = relative_volume(_prof(_BASELINE, 20), actual_cumulative=45_000_000, minute_of_session=389)
    assert r.value > 1.1 and r.method == "profile"


def test_missing_minute_falls_back_to_nearest_earlier_bucket() -> None:
    # No bucket at 200; baseline() should reuse minute 195's value.
    r = relative_volume(_prof(_BASELINE, 20), actual_cumulative=20_000_000, minute_of_session=200)
    assert r.value == 1.0 and r.method == "profile"


def test_insufficient_history_is_estimated_not_flat_equivalent(monkeypatch) -> None:
    from app.config import settings
    monkeypatch.setattr(settings, "short_duration_volume_profile_allow_fallback", True, raising=False)
    r = relative_volume(_prof(_BASELINE, 3), actual_cumulative=1_000_000, minute_of_session=30,
                        avg_daily_volume=40_000_000)
    assert r.method == "flat_estimate" and r.is_estimated
    assert r.data_quality <= 0.4  # a thin baseline is never presented as high quality


def test_no_history_no_fallback_is_unavailable(monkeypatch) -> None:
    from app.config import settings
    monkeypatch.setattr(settings, "short_duration_volume_profile_allow_fallback", False, raising=False)
    r = relative_volume(None, actual_cumulative=1_000_000, minute_of_session=30, avg_daily_volume=None)
    assert r.method == "unavailable" and r.value is None  # cannot inflate a score


def test_missing_relvol_never_inflates() -> None:
    # unavailable -> value None -> the scorer's missing-factor rule (0.25) applies, never 1.0.
    r = relative_volume(None, 1_000_000, 30)
    assert r.value is None


def test_extreme_outlier_session_does_not_move_median() -> None:
    # Median across sessions ignores one 10x spike; use raw per-session curves.
    from app.scheduling.clock import MarketClock
    from app.shortduration.volume_profile import _session_cumulative
    clock = MarketClock()
    bars: list[IntradayBar] = []
    # 5 normal sessions (~1M by minute 5) + 1 outlier (10M), all Mondays-Fridays.
    base_day = datetime(2026, 7, 6, 9, 30, tzinfo=_ET)  # a Monday
    for s in range(6):
        day = base_day + timedelta(days=s)
        vol = 10_000_000 if s == 5 else 1_000_000
        bars.append(IntradayBar(ts=day.astimezone(UTC), open=1, high=1, low=1, close=1, volume=vol))
    cum = _session_cumulative(bars, clock)
    import statistics
    vals = [c[0] for c in cum.values() if 0 in c]
    assert statistics.median(vals) == 1_000_000  # outlier ignored by the median


async def test_build_profile_via_mock_provider() -> None:
    # Mock intraday provider yields 20 synthetic sessions -> a real (non-estimated) profile.
    prof = await vp.build_volume_profile("AAPL", now=datetime(2026, 7, 20, 17, 0, tzinfo=UTC))
    assert prof.sessions_used >= 10 and not prof.is_estimated
    assert len(prof.minute_median) > 50 and prof.data_quality > 0.4
