"""FMP get_earnings: per-symbol, future-only, never another company's date.

Regression for the wrong-earnings bug: the earnings-calendar endpoint ignored its
?symbol parameter and returned the market-wide calendar, so the first row dated
>= today (a random other company) became every symbol's "next earnings" — the
positions board showed TSLA earnings "today" two days after TSLA had reported.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.providers.fmp.client import FMPProvider

_TODAY = datetime.now(UTC).date()
_D = lambda days: (_TODAY + timedelta(days=days)).isoformat()  # noqa: E731


class _FakeHTTP:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def get_json(self, path, params=None):
        self.calls.append((path, params))
        return self.rows


def _provider(rows) -> FMPProvider:
    p = FMPProvider.__new__(FMPProvider)  # skip network setup
    p._http = _FakeHTTP(rows)
    return p


async def test_ignores_other_symbols_even_if_endpoint_leaks_them() -> None:
    # The exact failure shape: a market-wide calendar with OTHER companies today,
    # while TSLA's own next report is months out.
    rows = [
        {"symbol": "0QKE.L", "date": _D(0), "epsActual": None},
        {"symbol": "DGNX", "date": _D(0), "epsActual": None},
        {"symbol": "TSLA", "date": _D(96), "epsActual": None, "epsEstimated": 0.48},
        {"symbol": "TSLA", "date": _D(-2), "epsActual": 0.33},  # already reported
    ]
    ev = await _provider(rows).get_earnings("TSLA")
    assert ev is not None
    assert ev.report_date == _TODAY + timedelta(days=96)  # TSLA's own, not 0QKE.L's


async def test_reported_today_is_not_upcoming() -> None:
    # A row dated today WITH an actual already filled has happened — it must not
    # resurrect as an "earnings before expiry" warning.
    rows = [
        {"symbol": "JPM", "date": _D(0), "epsActual": 4.12},
        {"symbol": "JPM", "date": _D(81), "epsActual": None},
    ]
    ev = await _provider(rows).get_earnings("JPM")
    assert ev is not None and ev.report_date == _TODAY + timedelta(days=81)


async def test_no_upcoming_returns_none() -> None:
    rows = [{"symbol": "XLF", "date": _D(-30), "epsActual": 1.0}]
    assert await _provider(rows).get_earnings("XLF") is None


async def test_uses_per_symbol_endpoint() -> None:
    p = _provider([])
    await p.get_earnings("TSLA")
    path, params = p._http.calls[0]
    assert path == "/stable/earnings"
    assert params == {"symbol": "TSLA"}
