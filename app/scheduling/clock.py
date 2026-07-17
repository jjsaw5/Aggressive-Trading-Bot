"""Eastern-Time market clock: which trading session are we in right now?

Sessions follow the platform's monitoring windows (all times ET):

    OVERNIGHT       20:00 - 07:00   news/filings gathering, slow
    PRE_MARKET      07:00 - 09:00   universe refresh, gap/relvol
    FINAL_PRE_OPEN  09:00 - 09:30   regime + final watchlist
    OPENING         09:30 - 09:45   opening-range stabilization
    PRIMARY         09:45 - 11:00   highest-priority trading window
    MIDDAY          11:00 - 14:00   reduced activity
    AFTERNOON       14:00 - 15:00   reassessment
    POWER_HOUR      15:00 - 16:00   hold/exit, overnight risk
    POST_CLOSE      close - close+30 reconciliation
    EARNINGS        close+30 - 20:00 after-hours earnings
    CLOSED          weekends / holidays

On early-close days the regular-hours close moves to 13:00, so MIDDAY runs to
the close and AFTERNOON/POWER_HOUR are skipped. DST is handled by zoneinfo — all
boundaries are ET wall-clock. Holidays and early closes come from a small,
maintained table (US equity market); extend it as years roll forward.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from enum import Enum
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


class MarketSession(str, Enum):
    OVERNIGHT = "overnight"
    PRE_MARKET = "pre_market"
    FINAL_PRE_OPEN = "final_pre_open"
    OPENING = "opening"
    PRIMARY = "primary"
    MIDDAY = "midday"
    AFTERNOON = "afternoon"
    POWER_HOUR = "power_hour"
    POST_CLOSE = "post_close"
    EARNINGS = "earnings"
    CLOSED = "closed"


# US equity-market full holidays (NYSE/Nasdaq), 2025-2027. Maintained by hand.
MARKET_HOLIDAYS: frozenset[str] = frozenset(
    {
        # 2025
        "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
        "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
        # 2026
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
        "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
        # 2027
        "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
        "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    }
)

# Early-close days (1:00 PM ET close): typically the day after Thanksgiving and
# Christmas Eve, plus July 3 when the 4th falls on a weekday.
EARLY_CLOSE_DAYS: frozenset[str] = frozenset(
    {
        "2025-07-03", "2025-11-28", "2025-12-24",
        "2026-11-27", "2026-12-24",
        "2027-11-26",
    }
)

_REGULAR_CLOSE = time(16, 0)
_EARLY_CLOSE = time(13, 0)


class MarketClock:
    def __init__(
        self,
        *,
        holidays: frozenset[str] = MARKET_HOLIDAYS,
        early_closes: frozenset[str] = EARLY_CLOSE_DAYS,
        tz: ZoneInfo = _ET,
    ) -> None:
        self.holidays = holidays
        self.early_closes = early_closes
        self.tz = tz

    def now_et(self, now: datetime | None = None) -> datetime:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return now.astimezone(self.tz)

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d.isoformat() not in self.holidays

    def close_time(self, d: date) -> time:
        return _EARLY_CLOSE if d.isoformat() in self.early_closes else _REGULAR_CLOSE

    def session(self, now: datetime | None = None) -> MarketSession:
        et = self.now_et(now)
        t = et.time()
        d = et.date()

        if not self.is_trading_day(d):
            # Weekends/holidays: overnight-style slow gathering, else closed.
            return MarketSession.CLOSED if (time(7, 0) <= t < time(20, 0)) else MarketSession.OVERNIGHT

        close = self.close_time(d)

        # Pre-open
        if time(7, 0) <= t < time(9, 0):
            return MarketSession.PRE_MARKET
        if time(9, 0) <= t < time(9, 30):
            return MarketSession.FINAL_PRE_OPEN

        # Regular hours
        if time(9, 30) <= t < time(9, 45):
            return MarketSession.OPENING
        if time(9, 45) <= t < time(11, 0):
            return MarketSession.PRIMARY
        if time(11, 0) <= t < close and t < time(14, 0):
            return MarketSession.MIDDAY
        if close > time(14, 0):  # full day only
            if time(14, 0) <= t < time(15, 0):
                return MarketSession.AFTERNOON
            if time(15, 0) <= t < close:
                return MarketSession.POWER_HOUR

        # Post-close / after-hours
        close_dt = time(close.hour, close.minute)
        post_end = time((close.hour + (close.minute + 30) // 60), (close.minute + 30) % 60)
        if close_dt <= t < post_end:
            return MarketSession.POST_CLOSE
        if post_end <= t < time(20, 0):
            return MarketSession.EARNINGS

        # Overnight (20:00 -> 07:00)
        return MarketSession.OVERNIGHT

    def is_market_open(self, now: datetime | None = None) -> bool:
        """True during regular trading hours (09:30 - close on a trading day)."""
        et = self.now_et(now)
        if not self.is_trading_day(et.date()):
            return False
        return time(9, 30) <= et.time() < self.close_time(et.date())
