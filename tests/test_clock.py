"""Market clock: ET session resolution, holidays, early closes, DST."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.scheduling.clock import MarketClock, MarketSession

_ET = ZoneInfo("America/New_York")


def _et(y, mo, d, h, mi) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=_ET)


def test_regular_trading_day_sessions() -> None:
    c = MarketClock()
    # Thursday 2026-07-16 is a normal trading day.
    assert c.session(_et(2026, 7, 16, 8, 0)) == MarketSession.PRE_MARKET
    assert c.session(_et(2026, 7, 16, 9, 15)) == MarketSession.FINAL_PRE_OPEN
    assert c.session(_et(2026, 7, 16, 9, 35)) == MarketSession.OPENING
    assert c.session(_et(2026, 7, 16, 10, 0)) == MarketSession.PRIMARY
    assert c.session(_et(2026, 7, 16, 12, 0)) == MarketSession.MIDDAY
    assert c.session(_et(2026, 7, 16, 14, 30)) == MarketSession.AFTERNOON
    assert c.session(_et(2026, 7, 16, 15, 30)) == MarketSession.POWER_HOUR
    assert c.session(_et(2026, 7, 16, 16, 15)) == MarketSession.POST_CLOSE
    assert c.session(_et(2026, 7, 16, 18, 0)) == MarketSession.EARNINGS
    assert c.session(_et(2026, 7, 16, 22, 0)) == MarketSession.OVERNIGHT
    assert c.is_market_open(_et(2026, 7, 16, 10, 0)) is True
    assert c.is_market_open(_et(2026, 7, 16, 17, 0)) is False


def test_weekend_is_closed_or_overnight() -> None:
    c = MarketClock()
    # Saturday 2026-07-18.
    assert c.is_trading_day(datetime(2026, 7, 18).date()) is False
    assert c.session(_et(2026, 7, 18, 11, 0)) == MarketSession.CLOSED
    assert c.session(_et(2026, 7, 18, 23, 0)) == MarketSession.OVERNIGHT


def test_holiday_is_not_a_trading_day() -> None:
    c = MarketClock()
    # Christmas 2026-12-25 (Friday) is a holiday.
    assert c.is_trading_day(datetime(2026, 12, 25).date()) is False
    assert c.session(_et(2026, 12, 25, 10, 0)) == MarketSession.CLOSED
    assert c.is_market_open(_et(2026, 12, 25, 10, 0)) is False


def test_early_close_day_shifts_close_and_skips_power_hour() -> None:
    c = MarketClock()
    # 2026-12-24 is an early-close day (13:00 ET close).
    d = datetime(2026, 12, 24).date()
    assert c.close_time(d).hour == 13
    assert c.session(_et(2026, 12, 24, 12, 30)) == MarketSession.MIDDAY
    assert c.session(_et(2026, 12, 24, 13, 15)) == MarketSession.POST_CLOSE
    # No power hour on an early-close day.
    assert c.session(_et(2026, 12, 24, 15, 30)) == MarketSession.EARNINGS
    assert c.is_market_open(_et(2026, 12, 24, 14, 0)) is False


def test_utc_input_is_converted_to_et() -> None:
    c = MarketClock()
    # 14:00 UTC on 2026-07-16 = 10:00 ET (EDT, UTC-4) -> PRIMARY.
    assert c.session(datetime(2026, 7, 16, 14, 0, tzinfo=UTC)) == MarketSession.PRIMARY
