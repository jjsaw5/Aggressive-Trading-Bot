"""Intraday session levels: VWAP, opening range, relative volume, prior-day and
premarket high/low — the primitives 0DTE/1-5DTE setups are built on.

Pure functions over `IntradayBar`s. No I/O: a caller fetches the bars (and the
optional reference inputs) and passes them in. Missing inputs yield `None`
levels, never fabricated values — an unset opening range means "not established
yet", which downstream gates must treat as a disqualifier, not as neutral.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from app.domain.shortduration import IntradayBar, IntradayLevels

_ET = ZoneInfo("America/New_York")
_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)


def _et(ts: datetime) -> datetime:
    return ts.astimezone(_ET)


def rth_bars(bars: list[IntradayBar]) -> list[IntradayBar]:
    """Bars within regular trading hours (09:30–16:00 ET), chronological."""
    out = [b for b in bars if _RTH_OPEN <= _et(b.ts).time() < _RTH_CLOSE]
    out.sort(key=lambda b: b.ts)
    return out


def vwap(bars: list[IntradayBar]) -> float | None:
    """Volume-weighted average of the typical price (H+L+C)/3."""
    num = den = 0.0
    for b in bars:
        typical = (b.high + b.low + b.close) / 3.0
        num += typical * b.volume
        den += b.volume
    return round(num / den, 4) if den > 0 else None


def opening_range(
    bars: list[IntradayBar], minutes: int, session_date: date
) -> tuple[float | None, float | None]:
    """High/low of the first `minutes` of RTH. Returns (None, None) until the
    opening-range window has fully elapsed (no premature breakout reads)."""
    start = datetime.combine(session_date, _RTH_OPEN, tzinfo=_ET)
    end = start + _td(minutes)
    window = [b for b in bars if start <= _et(b.ts) < end]
    if not window:
        return None, None
    # Only report once the window is complete — i.e. we have a bar at/after its end.
    last_et = _et(max(bars, key=lambda b: b.ts).ts) if bars else None
    if last_et is None or last_et < end:
        return None, None
    return round(max(b.high for b in window), 4), round(min(b.low for b in window), 4)


def _td(minutes: int):
    from datetime import timedelta

    return timedelta(minutes=minutes)


def relative_volume(
    bars: list[IntradayBar], session_date: date, avg_daily_volume: float | None
) -> float | None:
    """Cumulative RTH volume so far vs the volume expected by this point in the
    session, using a flat intraday distribution over the 390-minute session.

    This is a documented approximation (a true measure needs prior-day intraday
    profiles). Returns None when no average is available — we do not invent one.
    """
    if not bars or not avg_daily_volume or avg_daily_volume <= 0:
        return None
    open_dt = datetime.combine(session_date, _RTH_OPEN, tzinfo=_ET)
    last = max(bars, key=lambda b: b.ts)
    minutes_elapsed = max(1.0, (_et(last.ts) - open_dt).total_seconds() / 60.0)
    minutes_elapsed = min(minutes_elapsed, 390.0)
    expected = avg_daily_volume * (minutes_elapsed / 390.0)
    actual = sum(b.volume for b in bars)
    return round(actual / expected, 2) if expected > 0 else None


def compute_intraday_levels(
    symbol: str,
    bars: list[IntradayBar],
    *,
    session_date: date | None = None,
    opening_range_minutes: int = 15,
    prior_day_high: float | None = None,
    prior_day_low: float | None = None,
    premarket_high: float | None = None,
    premarket_low: float | None = None,
    avg_daily_volume: float | None = None,
    relative_volume_reading=None,
    now: datetime,
    source: str = "unknown",
) -> IntradayLevels:
    """Assemble an `IntradayLevels` from a symbol's intraday bars + references.

    `relative_volume_reading` (a volume_profile.RelativeVolumeReading) overrides
    the flat proration when the caller has built a time-of-day profile; its method
    and estimated flag are carried onto the levels for transparency."""
    d = session_date or _et(now).date()
    session = rth_bars(bars)
    last = session[-1].close if session else None
    or_high, or_low = opening_range(session, opening_range_minutes, d)
    if relative_volume_reading is not None:
        rv, rv_method, rv_est = (
            relative_volume_reading.value,
            relative_volume_reading.method,
            relative_volume_reading.is_estimated,
        )
    else:
        rv, rv_method, rv_est = relative_volume(session, d, avg_daily_volume), "flat_estimate", True
    return IntradayLevels(
        symbol=symbol.upper(),
        session_date=d,
        last=last,
        vwap=vwap(session),
        opening_range_high=or_high,
        opening_range_low=or_low,
        opening_range_minutes=opening_range_minutes,
        premarket_high=premarket_high,
        premarket_low=premarket_low,
        prior_day_high=prior_day_high,
        prior_day_low=prior_day_low,
        relative_volume=rv,
        relative_volume_method=rv_method if session else None,
        relative_volume_estimated=rv_est,
        computed_at=now,
        source=source,
    )
