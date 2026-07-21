"""Phase 1 — data-freshness policy (per state / track / use-case)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.enums import CandidateState, DTECategory
from app.shortduration.freshness import (
    evaluate_quote_freshness,
    threshold_seconds,
    timestamps_consistent,
    use_case_for_state,
)

_NOW = datetime(2026, 7, 20, 15, 0, tzinfo=UTC)


def _fresh(age_s, *, state, dte=DTECategory.ZERO_DTE, delayed=0, provider="fmp"):
    return evaluate_quote_freshness(
        as_of=_NOW - timedelta(seconds=age_s), delayed_minutes=delayed, now=_NOW,
        capability="underlying", state=state, dte=dte, provider=provider,
    )


def test_use_case_by_state() -> None:
    assert use_case_for_state(CandidateState.DETECTED) == "broad"
    assert use_case_for_state(CandidateState.WATCHLIST) == "watchlist"
    assert use_case_for_state(CandidateState.ARMED) == "armed"
    assert use_case_for_state(CandidateState.OPEN) == "open"


def test_thresholds_tighten_toward_trade_ready() -> None:
    b = threshold_seconds("underlying", "broad", DTECategory.ZERO_DTE)
    w = threshold_seconds("underlying", "watchlist", DTECategory.ZERO_DTE)
    a = threshold_seconds("underlying", "armed", DTECategory.ZERO_DTE)
    o = threshold_seconds("underlying", "open", DTECategory.ZERO_DTE)
    assert b > w > a > o  # broad 120 > watchlist 30 > armed 8 > open 5


def test_fresh_realtime_quote_passes() -> None:
    r = _fresh(3, state=CandidateState.ARMED)
    assert r.ok and r.use_case == "armed" and r.reason == "fresh"


def test_stale_option_quote_blocks_armed_0dte() -> None:
    # 20s old is fine for watchlist (30s) but stale for an armed 0DTE (8s).
    assert _fresh(20, state=CandidateState.WATCHLIST).ok is True
    assert _fresh(20, state=CandidateState.ARMED).ok is False


def test_provider_delayed_data_blocks() -> None:
    r = _fresh(2, state=CandidateState.ARMED, delayed=15)
    assert not r.ok and "delayed" in r.reason


def test_unknown_source_blocks_trade_ready() -> None:
    r = _fresh(2, state=CandidateState.ARMED, provider="unknown")
    assert not r.ok and "unknown" in r.reason
    r2 = evaluate_quote_freshness(as_of=_NOW, delayed_minutes=0, now=_NOW, capability="underlying",
                                  state=CandidateState.ARMED, dte=DTECategory.ZERO_DTE, provider=None)
    assert not r2.ok


def test_non_0dte_relaxes_armed_budget() -> None:
    # 1-5DTE armed uses the watchlist budget (30s), not the 8s 0DTE budget.
    assert _fresh(20, state=CandidateState.ARMED, dte=DTECategory.SHORT_DTE).ok is True


def test_missing_quote_is_not_fresh() -> None:
    r = evaluate_quote_freshness(as_of=None, delayed_minutes=None, now=_NOW, capability="underlying",
                                 state=CandidateState.ARMED, dte=DTECategory.ZERO_DTE, provider="fmp")
    assert not r.ok and "no quote" in r.reason


def test_clock_skew_between_underlying_and_option() -> None:
    assert timestamps_consistent(_NOW, _NOW - timedelta(seconds=10)) is True
    assert timestamps_consistent(_NOW, _NOW - timedelta(seconds=120)) is False


def test_market_closed_quote_still_evaluated_by_age() -> None:
    # Freshness is age-based; a very old quote (market closed) fails trade-ready.
    assert _fresh(3600, state=CandidateState.ARMED).ok is False
