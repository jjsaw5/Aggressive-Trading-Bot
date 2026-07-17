"""Scheduler cadence is configurable and defaults to a slow 3-hour baseline."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_scan_interval_defaults_to_three_hours() -> None:
    # Default baseline is 180 minutes (every 3 hours), not the old 15.
    s = Settings(_env_file=None)
    assert s.scan_interval_minutes == 180


def test_scan_interval_is_overridable() -> None:
    assert Settings(_env_file=None, scan_interval_minutes=60).scan_interval_minutes == 60


def test_scan_interval_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, scan_interval_minutes=0)


def test_scheduler_reads_configured_interval(monkeypatch) -> None:
    # The scheduler module must pull the cadence from settings, not a constant.
    from app.config import settings

    monkeypatch.setattr(settings, "scan_interval_minutes", 42)
    import app.scheduler.run as run

    assert run.settings.scan_interval_minutes == 42
