"""Historical time-of-day intraday volume profile.

Replaces the flat-proration relative-volume proxy with a real intraday volume
curve: for each minute-of-session, the *median* cumulative volume across the last
N completed trading sessions. Relative volume at minute M is then

    actual cumulative volume through M  /  median historical cumulative through M

Median (not mean) is preferred so a single unusual-event session (earnings, halt)
doesn't distort the baseline. The profile is holiday / early-close / missing-bar
aware via `MarketClock`, and degrades honestly: when fewer than the configured
minimum sessions exist it is marked ESTIMATED (or the reading is UNAVAILABLE) —
never silently presented as equivalent-quality data, and a missing reading can
never inflate a score.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.config import settings
from app.domain.shortduration import IntradayBar
from app.logging_config import get_logger
from app.providers import registry
from app.scheduling.clock import MarketClock

log = get_logger(__name__)
_ET = ZoneInfo("America/New_York")
_OPEN = time(9, 30)


class IntradayVolumeProfile(BaseModel):
    """Per-minute-of-session historical cumulative-volume baseline for one symbol."""

    symbol: str
    as_of_date: date
    sessions_used: int
    minute_median: dict[int, float] = Field(default_factory=dict)  # minute -> median cum vol
    minute_mean: dict[int, float] = Field(default_factory=dict)
    minute_samples: dict[int, int] = Field(default_factory=dict)   # minute -> #sessions with data
    is_estimated: bool = False       # < min usable sessions -> baseline is weak
    data_quality: float = 0.0        # [0,1], grows with sessions used
    updated_at: datetime

    def baseline(self, minute: int) -> float | None:
        src = self.minute_median if settings.short_duration_volume_profile_use_median else self.minute_mean
        # Fall back to the nearest earlier minute that has a baseline (missing bars).
        for m in range(minute, -1, -1):
            v = src.get(m)
            if v:
                return v
        return None


class RelativeVolumeReading(BaseModel):
    """A relative-volume reading with full provenance so scoring can trust it."""

    value: float | None
    method: str                  # "profile" | "flat_estimate" | "unavailable"
    minute_of_session: int | None = None
    actual_cumulative: float | None = None
    expected_cumulative: float | None = None
    sessions_used: int = 0
    is_estimated: bool = False
    data_quality: float = 0.0
    note: str = ""


def _minute_of_session(ts: datetime) -> int:
    et = ts.astimezone(_ET)
    return int((datetime.combine(et.date(), et.timetz().replace(tzinfo=None))
                - datetime.combine(et.date(), _OPEN)).total_seconds() // 60)


def _session_cumulative(bars: list[IntradayBar], clock: MarketClock) -> dict[date, dict[int, float]]:
    """Group RTH bars by ET session date; per session, cumulative volume by
    minute-of-session (0 = 09:30). Bars outside RTH or on non-trading days dropped."""
    by_session: dict[date, list[tuple[int, float]]] = defaultdict(list)
    for b in bars:
        et = b.ts.astimezone(_ET)
        d = et.date()
        if not clock.is_trading_day(d):
            continue
        m = _minute_of_session(b.ts)
        close_min = int((datetime.combine(d, clock.close_time(d)) - datetime.combine(d, _OPEN)).total_seconds() // 60)
        if m < 0 or m >= close_min:  # keep only regular-hours minutes for that day's close
            continue
        by_session[d].append((m, b.volume or 0.0))
    cum: dict[date, dict[int, float]] = {}
    for d, rows in by_session.items():
        rows.sort()
        running = 0.0
        per_min: dict[int, float] = {}
        for m, v in rows:
            running += v
            per_min[m] = running
        cum[d] = per_min
    return cum


async def build_volume_profile(
    symbol: str, *, now: datetime | None = None, clock: MarketClock | None = None,
) -> IntradayVolumeProfile:
    """Fetch the lookback window of 1-min bars and build the per-minute median
    cumulative-volume baseline. Excludes today (incomplete) — only *completed*
    sessions inform the baseline."""
    now = now or datetime.now(UTC)
    clock = clock or MarketClock()
    symbol = symbol.upper()
    lookback = settings.short_duration_volume_profile_sessions
    today = now.astimezone(_ET).date()
    # Pull a generous calendar window to net `lookback` trading sessions. The
    # baseline is built from 5-min bars: providers commonly serve far more history
    # at 5-min than 1-min (e.g. FMP: ~1 session of 1-min vs ~2 weeks of 5-min), and
    # 5-min granularity is ample for a cumulative-volume baseline. `baseline()` maps
    # a live 1-min minute-of-session to the nearest 5-min bucket.
    from_date = today - timedelta(days=lookback * 2 + 14)
    to_date = today - timedelta(days=1)  # completed sessions only
    bars = await registry.intraday_provider().get_intraday_bars(
        symbol, interval="5min", from_date=from_date, to_date=to_date
    )
    cum = _session_cumulative(bars, clock)
    # Keep the most recent `lookback` completed sessions.
    sessions = sorted(cum)[-lookback:]
    per_minute: dict[int, list[float]] = defaultdict(list)
    for d in sessions:
        for m, v in cum[d].items():
            per_minute[m].append(v)
    minute_median = {m: round(statistics.median(vs), 2) for m, vs in per_minute.items() if vs}
    minute_mean = {m: round(statistics.fmean(vs), 2) for m, vs in per_minute.items() if vs}
    minute_samples = {m: len(vs) for m, vs in per_minute.items()}
    n = len(sessions)
    min_sessions = settings.short_duration_volume_profile_min_sessions
    dq = round(min(1.0, n / max(1, lookback)), 3)
    prof = IntradayVolumeProfile(
        symbol=symbol, as_of_date=today, sessions_used=n,
        minute_median=minute_median, minute_mean=minute_mean, minute_samples=minute_samples,
        is_estimated=n < min_sessions, data_quality=dq, updated_at=now,
    )
    log.info("volume_profile_built", symbol=symbol, sessions=n, estimated=prof.is_estimated)
    return prof


# --- In-memory profile cache (profiles are stable within a trading day) --------
_CACHE: dict[str, tuple[datetime, IntradayVolumeProfile]] = {}


async def get_or_build_profile(
    symbol: str, *, now: datetime | None = None, clock: MarketClock | None = None,
) -> IntradayVolumeProfile:
    now = now or datetime.now(UTC)
    key = symbol.upper()
    ttl = timedelta(minutes=settings.short_duration_volume_profile_cache_minutes)
    hit = _CACHE.get(key)
    if hit and (now - hit[0]) < ttl and hit[1].as_of_date == now.astimezone(_ET).date():
        return hit[1]
    prof = await build_volume_profile(symbol, now=now, clock=clock)
    _CACHE[key] = (now, prof)
    return prof


async def relative_volume_now(
    symbol: str, session_bars: list[IntradayBar], *,
    now: datetime | None = None, clock: MarketClock | None = None,
    avg_daily_volume: float | None = None,
) -> RelativeVolumeReading:
    """Build/reuse the symbol's profile and read relative volume against today's
    cumulative RTH volume. Best-effort: on any provider failure, degrades to the
    labelled flat estimate (or unavailable)."""
    now = now or datetime.now(UTC)
    actual = sum(b.volume or 0.0 for b in session_bars)
    minute = _minute_of_session(max(session_bars, key=lambda b: b.ts).ts) if session_bars else 0
    profile: IntradayVolumeProfile | None = None
    if settings.short_duration_use_volume_profile:
        try:
            profile = await get_or_build_profile(symbol, now=now, clock=clock)
        except Exception as exc:  # noqa: BLE001 - fall back honestly, never fabricate
            log.warning("volume_profile_failed", symbol=symbol, error=str(exc))
    return relative_volume(profile, actual, minute, avg_daily_volume=avg_daily_volume)


def relative_volume(
    profile: IntradayVolumeProfile | None,
    actual_cumulative: float,
    minute_of_session: int,
    *, avg_daily_volume: float | None = None,
) -> RelativeVolumeReading:
    """Relative volume from the profile; honest fallback when history is thin.

    Order of preference: (1) real profile with >= min sessions; (2) a clearly
    LABELLED flat estimate when a profile is thin/absent and fallback is allowed
    and an avg daily volume exists; (3) unavailable. A thin/absent profile never
    silently reads as a normal 1.0x."""
    min_sessions = settings.short_duration_volume_profile_min_sessions
    if profile and profile.sessions_used >= min_sessions:
        base = profile.baseline(minute_of_session)
        if base and base > 0:
            return RelativeVolumeReading(
                value=round(actual_cumulative / base, 2), method="profile",
                minute_of_session=minute_of_session, actual_cumulative=round(actual_cumulative, 0),
                expected_cumulative=round(base, 0), sessions_used=profile.sessions_used,
                is_estimated=False, data_quality=profile.data_quality,
                note=f"{profile.sessions_used}-session median profile",
            )
    if settings.short_duration_volume_profile_allow_fallback and avg_daily_volume and avg_daily_volume > 0:
        frac = min(1.0, max(1.0, minute_of_session) / 390.0)
        expected = avg_daily_volume * frac
        n = profile.sessions_used if profile else 0
        return RelativeVolumeReading(
            value=round(actual_cumulative / expected, 2) if expected else None,
            method="flat_estimate", minute_of_session=minute_of_session,
            actual_cumulative=round(actual_cumulative, 0), expected_cumulative=round(expected, 0),
            sessions_used=n, is_estimated=True, data_quality=round(min(0.4, 0.02 * n), 3),
            note=f"estimated — flat proration ({n} sessions of history, need {min_sessions})",
        )
    return RelativeVolumeReading(
        value=None, method="unavailable", minute_of_session=minute_of_session,
        is_estimated=True, sessions_used=profile.sessions_used if profile else 0,
        note="no usable volume profile and fallback unavailable",
    )
