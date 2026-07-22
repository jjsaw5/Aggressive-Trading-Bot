"""Phase 4: the periodic scanner is session-aware — it idles a dark market."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.scheduler.run import _market_is_dark

_ET = ZoneInfo("America/New_York")


def test_overnight_and_weekend_are_dark() -> None:
    assert _market_is_dark(datetime(2026, 7, 18, 3, 0, tzinfo=_ET))   # Sat 3am
    assert _market_is_dark(datetime(2026, 7, 15, 2, 30, tzinfo=_ET))  # Wed overnight


def test_regular_and_extended_sessions_are_not_dark() -> None:
    assert not _market_is_dark(datetime(2026, 7, 15, 11, 0, tzinfo=_ET))  # RTH
    assert not _market_is_dark(datetime(2026, 7, 15, 8, 0, tzinfo=_ET))   # pre-market
    assert not _market_is_dark(datetime(2026, 7, 15, 17, 0, tzinfo=_ET))  # post-close
