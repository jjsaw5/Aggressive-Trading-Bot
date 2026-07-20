"""Actionability ranking for the short-duration candidate board."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.enums import CandidateState, Direction, DTECategory, ShortDurationStrategy
from app.domain.shortduration import ShortDurationCandidate
from app.shortduration import ranking

_NOW = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)


def _cand(**kw) -> ShortDurationCandidate:
    base = dict(
        id=kw.pop("id", "x"), symbol=kw.pop("symbol", "SPY"),
        dte_category=kw.pop("dte", DTECategory.ZERO_DTE),
        detected_at=kw.pop("detected_at", _NOW),
        state=kw.pop("state", CandidateState.EVALUATING),
    )
    base.update(kw)
    return ShortDurationCandidate(**base)


def test_bucket_assignment() -> None:
    assert ranking.bucket(_cand(state=CandidateState.ARMED, entry_allowed=True)) == ranking.READY
    assert ranking.bucket(_cand(state=CandidateState.ARMED, entry_allowed=False)) == ranking.ARMED_BLOCKED
    assert ranking.bucket(_cand(state=CandidateState.TRIGGERED, entry_allowed=True)) == ranking.READY
    assert ranking.bucket(_cand(state=CandidateState.WATCHLIST)) == ranking.WATCHLIST
    assert ranking.bucket(_cand(state=CandidateState.OPEN)) == ranking.IN_FLIGHT
    assert ranking.bucket(_cand(state=CandidateState.EVALUATING)) == ranking.EVALUATING
    assert ranking.bucket(_cand(state=CandidateState.REJECTED)) == ranking.TERMINAL


def test_ready_outranks_higher_score_that_is_blocked_or_watchlist() -> None:
    # Distinct symbols so dedupe (same symbol+strategy+dte) doesn't collapse them.
    ready = _cand(id="ready", symbol="SPY", state=CandidateState.ARMED, entry_allowed=True, score=0.50)
    blocked = _cand(id="blocked", symbol="QQQ", state=CandidateState.ARMED, entry_allowed=False, score=0.99)
    watch = _cand(id="watch", symbol="IWM", state=CandidateState.WATCHLIST, score=0.95)
    rejected = _cand(id="rej", symbol="AAPL", state=CandidateState.REJECTED, score=0.99)
    out = ranking.rank_candidates([rejected, watch, blocked, ready])
    assert [c.id for c in out] == ["ready", "blocked", "watch", "rej"]


def test_within_bucket_sorts_by_score_then_rr() -> None:
    a = _cand(id="a", symbol="A", state=CandidateState.WATCHLIST, score=0.60, reward_to_risk=1.0)
    b = _cand(id="b", symbol="B", state=CandidateState.WATCHLIST, score=0.80, reward_to_risk=1.0)
    c = _cand(id="c", symbol="C", state=CandidateState.WATCHLIST, score=0.80, reward_to_risk=3.0)
    out = ranking.rank_candidates([a, b, c])
    assert [x.id for x in out] == ["c", "b", "a"]  # score first, then R:R breaks the 0.80 tie


def test_dedupe_keeps_freshest_per_setup() -> None:
    older = _cand(id="old", symbol="SPY", strategy=ShortDurationStrategy.OPENING_RANGE_BREAKOUT,
                  direction=Direction.BULLISH, state=CandidateState.WATCHLIST,
                  score=0.9, detected_at=_NOW - timedelta(minutes=10))
    newer = _cand(id="new", symbol="SPY", strategy=ShortDurationStrategy.OPENING_RANGE_BREAKOUT,
                  direction=Direction.BULLISH, state=CandidateState.ARMED, entry_allowed=True,
                  score=0.5, detected_at=_NOW)
    out = ranking.rank_candidates([older, newer])
    assert [c.id for c in out] == ["new"]  # same (symbol, strategy, dte) -> only freshest


def test_dedupe_off_preserves_all() -> None:
    a = _cand(id="a", strategy=ShortDurationStrategy.OPENING_RANGE_BREAKOUT, state=CandidateState.WATCHLIST)
    b = _cand(id="b", strategy=ShortDurationStrategy.OPENING_RANGE_BREAKOUT, state=CandidateState.WATCHLIST,
              detected_at=_NOW - timedelta(minutes=5))
    assert len(ranking.rank_candidates([a, b], dedupe=False)) == 2
