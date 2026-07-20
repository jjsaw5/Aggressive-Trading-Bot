"""Live symbol search — the on-demand single-symbol research report."""

from __future__ import annotations

from app.research import symbol as symmod
from app.research.symbol import build_symbol_report


async def test_report_populates_all_sections_and_plays() -> None:
    """The aggregator fills market context + activity and runs BOTH play engines
    (short-duration + core swing) for the symbol, with no section errors on mocks."""
    r = await build_symbol_report("nvda")  # lower-case in -> normalized out
    assert r.symbol == "NVDA"
    assert r.quote is not None and r.quote.price > 0
    assert r.flow.alerts >= 0 and r.flow.calls + r.flow.puts == r.flow.alerts
    # Both engines ran (mock chain yields tradeable structures) and produced plays.
    assert (len(r.zero_dte) + len(r.one_five_dte)) > 0
    assert isinstance(r.swing, list)
    assert r.errors == {}


async def test_any_ticker_not_restricted_to_universe() -> None:
    """Search works for a symbol that is not in the scan universe."""
    r = await build_symbol_report("COIN")
    assert r.symbol == "COIN"
    assert r.quote is not None  # provider serves it regardless of the scan universe


async def test_section_failure_is_best_effort(monkeypatch) -> None:
    """A failing provider records an error for that section; the rest still returns."""
    # Keep the test fast + isolate the failure: stub the play engines to no-op.
    async def _no_detect(dte, *, now=None, universe=None):
        return []

    async def _no_scan(universe=None, portfolio=None):
        return []

    monkeypatch.setattr("app.shortduration.detection.run_detection", _no_detect)
    monkeypatch.setattr("app.services.scan_service.run_scan", _no_scan)

    from app.providers import registry
    market = registry.market_data_provider()

    async def _boom(_symbol):
        raise RuntimeError("provider down")

    monkeypatch.setattr(market, "get_quote", _boom)

    r = await build_symbol_report("AAPL")
    assert "quote" in r.errors and "provider down" in r.errors["quote"]
    assert r.quote is None
    # Other sections are unaffected.
    assert r.iv is not None or "iv" in r.errors  # iv still attempted
    assert r.news is not None


def test_route_rejects_bad_symbol() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    assert c.get("/research/symbol/123").status_code == 400
    assert c.get("/research/symbol/TOOLONGSYM").status_code == 400


def test_section_timeout_is_short_enough_to_be_useful() -> None:
    # Guards against an accidentally huge timeout that would hang the UI.
    assert symmod._SECTION_TIMEOUT_S <= 30
