"""Funnel orchestration: one pass promotes across tiers and persists membership."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import repository
from app.engine.universe import UniverseConfig
from app.main import app
from app.providers.mock import MockProvider
from app.tiers.funnel import FunnelEngine
from app.tiers.models import Tier, TierMember
from app.tiers.store import TierStore

_SYMS = ["SPY", "AAPL", "NVDA", "AMD", "QQQ"]

client = TestClient(app)


async def test_tier_store_replace_is_atomic() -> None:
    store = TierStore()
    await store.replace(Tier.WATCHLIST, [
        TierMember(symbol="AAA", tier=Tier.WATCHLIST, score=0.9),
        TierMember(symbol="BBB", tier=Tier.WATCHLIST, score=0.5),
    ])
    syms = await store.symbols(Tier.WATCHLIST)
    assert set(syms) == {"AAA", "BBB"}
    # Replace with a smaller set -> the dropped symbol is demoted (gone).
    await store.replace(Tier.WATCHLIST, [
        TierMember(symbol="AAA", tier=Tier.WATCHLIST, score=0.95),
    ])
    assert await store.symbols(Tier.WATCHLIST) == ["AAA"]


async def test_funnel_run_once_promotes_and_persists() -> None:
    mock = MockProvider()
    engine = FunnelEngine(
        market=mock, fundamentals=mock, calendar=mock, flow=mock, chain=mock,
        iv_history=mock, universe=UniverseConfig(symbols=_SYMS),
        watchlist_max=4, candidates_max=2,
    )
    report = await engine.run_once()
    assert report.tier1_evaluated == len(_SYMS)
    assert len(report.watchlist) <= 4
    assert len(report.candidates) <= 2
    # Candidates are a subset of the watchlist (funnel narrows).
    assert set(report.candidates) <= set(report.watchlist)

    # Membership persisted to the store.
    members = repository.list_all_tiers()
    tiers_present = {int(m.tier) for m in members}
    assert int(Tier.BROAD) in tiers_present
    assert int(Tier.WATCHLIST) in tiers_present


def test_tiers_api_run_and_list() -> None:
    r = client.post("/tiers/run", json={"symbols": _SYMS})
    assert r.status_code == 200
    body = r.json()
    assert body["tier1_evaluated"] == len(_SYMS)
    assert "watchlist" in body and "candidates" in body

    listing = client.get("/tiers")
    assert listing.status_code == 200
    data = listing.json()
    assert {"broad", "watchlist", "candidates", "positions"} <= set(data)
