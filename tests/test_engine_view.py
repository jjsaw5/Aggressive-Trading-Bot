"""Engine view: structural invalidation suggestion, entry stamp, engine picks.

The gap these close: the warehouse held 5,000 recorded engine decisions and
zero recorded views on the trades actually TAKEN, so the app could never be
graded on real positions — and every synced position started life with no
structural exit trigger at all.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.analytics.engine_view import suggest_invalidation
from app.domain.enums import (
    CandidateState,
    Direction,
    DTECategory,
)
from app.domain.shortduration import ContractRecommendation, ShortDurationCandidate
from app.shortduration.ranking import ENGINE_PICK_LIMIT, mark_engine_picks

_NOW = datetime(2026, 7, 28, 14, 30, tzinfo=UTC)


# --- Structural invalidation suggestion ---------------------------------------
def test_bearish_position_is_invalidated_on_a_close_back_above_the_mean() -> None:
    # Price below SMA20 — the level ahead of it is SMA20.
    s = suggest_invalidation(Direction.BEARISH, {"price": 340.0, "sma20": 353.5, "sma50": 366.0})
    assert s is not None
    assert s.price == 353.5 and s.source == "sma20"
    assert not s.already_breached
    assert "back above SMA20" in s.note


def test_bullish_position_mirrors_the_side() -> None:
    s = suggest_invalidation(Direction.BULLISH, {"price": 360.0, "sma20": 353.5, "sma50": 340.0})
    assert s is not None and s.price == 353.5
    assert not s.already_breached and "back below SMA20" in s.note


def test_an_already_breached_level_is_reported_not_quietly_widened() -> None:
    # Bearish but price has ALREADY closed above SMA20: say the structural
    # support is gone rather than silently picking a further level.
    s = suggest_invalidation(Direction.BEARISH, {"price": 360.0, "sma20": 353.5, "sma50": 366.0})
    assert s is not None
    assert s.already_breached is True
    assert "ALREADY" in s.note
    # SMA50 is still ahead of price, so it's offered as the next level out.
    assert s.alternate_price == 366.0 and s.alternate_source == "sma50"


def test_no_alternate_when_price_is_past_both_means() -> None:
    s = suggest_invalidation(Direction.BEARISH, {"price": 380.0, "sma20": 353.5, "sma50": 366.0})
    assert s is not None and s.already_breached and s.alternate_price is None


def test_suggestion_abstains_without_data_or_a_side() -> None:
    assert suggest_invalidation(Direction.BEARISH, None) is None
    assert suggest_invalidation(Direction.BEARISH, {"price": 340.0}) is None  # no sma20
    assert suggest_invalidation(Direction.NEUTRAL, {"price": 340.0, "sma20": 353.5}) is None


# --- Engine picks -------------------------------------------------------------
def _cand(sym: str, *, state=CandidateState.WATCHLIST, allowed=True, tradeable=True,
          score=0.7, reject: list[str] | None = None) -> ShortDurationCandidate:
    contract = ContractRecommendation(
        description="Put Debit Spread x1",
        legs=[{"option_type": "put", "strike": 230.0, "expiration": "2026-08-21"}],
    ) if tradeable else None
    return ShortDurationCandidate(
        id=f"c-{sym}", symbol=sym, dte_category=DTECategory.SHORT_DTE,
        detected_at=_NOW, state=state, entry_allowed=allowed, score=score,
        contract=contract, reject_reasons=reject or [],
    )


def test_engine_commits_to_a_short_ranked_pick_list() -> None:
    ranked = [_cand(s) for s in ("AAPL", "MSFT", "NVDA", "AMZN", "TSLA")]
    picked = mark_engine_picks(ranked)
    assert len(picked) == ENGINE_PICK_LIMIT  # a list long enough to include
    assert [c.symbol for c in picked] == ["AAPL", "MSFT", "NVDA"]  # ...everything
    assert [c.pick_rank for c in picked] == [1, 2, 3]  # isn't a prediction
    assert all(c.engine_pick for c in picked)
    # Board order is respected and the rest stay context, not picks.
    assert not any(c.engine_pick for c in ranked[ENGINE_PICK_LIMIT:])


def test_a_pick_must_be_actionable_not_merely_high_ranked() -> None:
    ranked = [
        _cand("RISKBLOCK", allowed=False, reject=["portfolio_limit"]),  # own risk rules refuse it
        _cand("NOSTRUCT", tradeable=False),  # nothing sized to trade
        _cand("DEAD", state=CandidateState.REJECTED),
        _cand("GOOD"),
    ]
    picked = mark_engine_picks(ranked)
    assert [c.symbol for c in picked] == ["GOOD"]


def test_a_closed_market_does_not_empty_the_pick_list() -> None:
    # "Market is closed" says nothing about whether the setup is good. Blocking
    # on it would leave the list empty exactly when planning the next session —
    # which is what happened on the first live run.
    ranked = [_cand("AAPL", allowed=False, reject=["time_of_day_blocked"])]
    picked = mark_engine_picks(ranked)
    assert [c.symbol for c in picked] == ["AAPL"]
    assert "gated until the session opens" in picked[0].pick_reason


def test_a_risk_block_still_disqualifies_even_alongside_a_timing_block() -> None:
    ranked = [_cand("X", allowed=False, reject=["time_of_day_blocked", "portfolio_limit"])]
    assert mark_engine_picks(ranked) == []


def test_picks_are_one_per_underlying() -> None:
    # The same symbol usually offers both a long leg and a vertical; spending
    # two of three slots on one name is a weaker commitment than three names.
    ranked = [_cand("QQQ"), _cand("QQQ"), _cand("IWM"), _cand("SPY")]
    ranked[1].id = "c-QQQ-2"
    picked = mark_engine_picks(ranked)
    assert [c.symbol for c in picked] == ["QQQ", "IWM", "SPY"]


def test_pick_reason_states_it_is_uncalibrated() -> None:
    picked = mark_engine_picks([_cand("AAPL")])
    assert "UNCALIBRATED" in picked[0].pick_reason


# --- Entry stamp (end to end) -------------------------------------------------
async def test_entry_stamps_the_engine_view_and_adopts_its_level(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    import app.api.routes.positions as pos
    import app.main as m
    from app.analytics.engine_view import EngineView
    from app.db import repository

    async def _fake_view(symbol, direction):
        return EngineView(
            as_of=_NOW, engine_direction="bullish",
            agrees_with_position=(direction == Direction.BULLISH),
            price=340.0, sma20=353.5, sma50=366.0, rsi=42.0,
            rationale="px 340.00, SMA20 353.50, SMA50 366.00, RSI 42.",
            invalidation_price=353.5, invalidation_source="sma20",
            invalidation_note="Daily close back above SMA20 (353.5).",
        )

    monkeypatch.setattr(pos, "build_engine_view", _fake_view, raising=False)
    monkeypatch.setattr("app.analytics.engine_view.build_engine_view", _fake_view)

    client = TestClient(m.app)
    r = client.post("/positions/quick-add", json={"line": "TSLA 370/365p 8/21 @2.45 x1"})
    assert r.status_code == 200, r.text
    t = repository.get_paper_trade(r.json()["id"])
    try:
        ev = t.trade_plan.engine_view
        assert ev is not None
        assert ev.engine_direction == "bullish"
        # The position is BEARISH, so the engine's bullish read disagrees — the
        # whole point of stamping it.
        assert ev.agrees_with_position is False
        # No level was given, so the structural suggestion is adopted: the
        # position does not start life manageable only by P&L.
        assert t.trade_plan.risk.invalidation_price == 353.5
    finally:
        repository.delete_paper_trade(t.id)


async def test_a_users_own_level_is_never_overwritten_by_the_suggestion(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    import app.main as m
    from app.analytics.engine_view import EngineView
    from app.db import repository

    async def _fake_view(symbol, direction):
        return EngineView(as_of=_NOW, engine_direction="bearish",
                          invalidation_price=353.5, invalidation_source="sma20")

    monkeypatch.setattr("app.analytics.engine_view.build_engine_view", _fake_view)
    client = TestClient(m.app)
    tid = client.post("/positions/quick-add",
                      json={"line": "TSLA 370/365p 8/21 @2.45 x1 inv 380"}).json()["id"]
    t = repository.get_paper_trade(tid)
    try:
        assert t.trade_plan.risk.invalidation_price == 380.0  # yours, not 353.5
        assert t.trade_plan.engine_view is not None  # still stamped
    finally:
        repository.delete_paper_trade(tid)


async def test_a_feed_failure_never_blocks_an_entry(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    import app.main as m
    from app.db import repository

    async def _boom(symbol, direction):
        raise RuntimeError("provider down")

    monkeypatch.setattr("app.analytics.engine_view.build_engine_view", _boom)
    client = TestClient(m.app)
    r = client.post("/positions/quick-add", json={"line": "AMD 200/190p 8/21 @2.00 x1"})
    assert r.status_code == 200, r.text
    t = repository.get_paper_trade(r.json()["id"])
    try:
        assert t.trade_plan.engine_view is None  # abstained, didn't guess
    finally:
        repository.delete_paper_trade(t.id)


def test_refresh_endpoint_backfills_only_unstamped_positions(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    import app.main as m
    from app.analytics.engine_view import EngineView
    from app.db import repository

    calls: list[str] = []

    async def _fake_view(symbol, direction):
        calls.append(symbol)
        return EngineView(as_of=_NOW, engine_direction="bearish",
                          agrees_with_position=True, invalidation_price=400.0,
                          invalidation_source="sma20")

    monkeypatch.setattr("app.analytics.engine_view.build_engine_view", _fake_view)
    client = TestClient(m.app)
    tid = client.post("/positions/quick-add",
                      json={"line": "META 700/690p 8/21 @1.50 x1"}).json()["id"]
    try:
        # Simulate a position imported before entry-stamping existed.
        t = repository.get_paper_trade(tid)
        t.trade_plan.engine_view = None
        repository.save_paper_trade(t)
        calls.clear()

        first = client.post("/positions/refresh-engine-view").json()
        assert "META" in first["stamped"]
        n_after_first = len(calls)
        # A second pass must not re-stamp what already carries a view.
        second = client.post("/positions/refresh-engine-view").json()
        assert "META" not in second["stamped"]
        assert len(calls) == n_after_first
    finally:
        repository.delete_paper_trade(tid)
