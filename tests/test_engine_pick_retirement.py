"""A pick list is a commitment to NOW, so the previous one must retire.

Regression for a board that read as uniformly bearish on a +3.2% QQQ day. The
engine_pick flag was write-only — every scan set it on up to three candidates
and nothing ever cleared the prior scan's — so flags accumulated across sessions.
A board committing to three was serving ten, four of them claiming rank=1, the
oldest 36 hours old. The backlog was dominated by an older bearish session, which
is precisely what the human saw.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.enums import (
    CandidateState,
    Direction,
    DTECategory,
    ShortDurationRegime,
    ShortDurationStrategy,
)
from app.domain.shortduration import ContractRecommendation, ShortDurationCandidate
from app.shortduration.ranking import (
    mark_engine_picks,
    rank_candidates,
    retire_engine_picks,
)

_NOW = datetime(2026, 7, 30, 14, 30, tzinfo=UTC)


def _contract(symbol: str) -> ContractRecommendation:
    # A pick requires a sized structure — mark_engine_picks refuses a bare setup.
    return ContractRecommendation(
        description="Call Debit Spread x1",
        legs=[{"action": "buy_to_open", "option_type": "call", "strike": 100.0,
               "expiration": "2026-08-21", "quantity": 1, "entry_price": 2.0},
              {"action": "sell_to_open", "option_type": "call", "strike": 105.0,
               "expiration": "2026-08-21", "quantity": 1, "entry_price": 0.5}],
        max_loss_usd=150.0, max_profit_usd=350.0,
    )


def _cand(cid: str, symbol: str, *, direction=Direction.BULLISH,
          state=CandidateState.WATCHLIST, score=0.7, age_min=0,
          picked=False, rank=None) -> ShortDurationCandidate:
    c = ShortDurationCandidate(
        id=cid, symbol=symbol, dte_category=DTECategory.ZERO_DTE,
        strategy=ShortDurationStrategy.OPENING_RANGE_BREAKOUT, direction=direction,
        detected_at=_NOW - timedelta(minutes=age_min),
        regime=ShortDurationRegime.RANGE_BOUND, score=score, confidence=score,
        state=state, contract=_contract(symbol), entry_allowed=True,
    )
    c.engine_pick = picked
    c.pick_rank = rank
    c.pick_reason = "prior scan" if picked else ""
    return c


# --- Retiring ------------------------------------------------------------------
def test_a_prior_scans_picks_are_cleared() -> None:
    prior = [_cand("a", "TSLA", picked=True, rank=1, age_min=1206),
             _cand("b", "IWM", picked=True, rank=3, age_min=1206)]
    retired = retire_engine_picks(prior)
    assert len(retired) == 2
    assert all(not c.engine_pick and c.pick_rank is None and not c.pick_reason
               for c in prior)


def test_the_current_scans_picks_survive_retirement() -> None:
    rows = [_cand("old", "TSLA", picked=True, rank=1, age_min=1206),
            _cand("new", "QQQ", picked=True, rank=1, age_min=0)]
    retired = retire_engine_picks(rows, keep_ids={"new"})
    assert [c.id for c in retired] == ["old"]
    assert rows[1].engine_pick is True and rows[1].pick_rank == 1


def test_only_changed_rows_are_returned_so_writes_stay_minimal() -> None:
    rows = [_cand("a", "TSLA", picked=True, rank=1), _cand("b", "IWM", picked=False)]
    assert [c.id for c in retire_engine_picks(rows)] == ["a"]


def test_retiring_an_already_clean_board_is_a_no_op() -> None:
    assert retire_engine_picks([_cand("a", "SPY"), _cand("b", "QQQ")]) == []


# --- A full scan cycle ---------------------------------------------------------
def test_a_second_scan_leaves_exactly_one_scans_worth_of_picks() -> None:
    # THE regression, end to end: yesterday's three bearish picks plus today's
    # three bullish ones must not both be live.
    yesterday = [_cand(f"y{i}", s, direction=Direction.BEARISH, picked=True,
                       rank=i + 1, age_min=1200, score=0.8 - i * 0.01)
                 for i, s in enumerate(["TSLA", "IWM", "NVDA"])]
    today = [_cand(f"t{i}", s, score=0.8 - i * 0.01)
             for i, s in enumerate(["QQQ", "SPY", "MSFT"])]

    retire_engine_picks(yesterday + today, keep_ids={c.id for c in today})
    picked = mark_engine_picks(rank_candidates(today, dedupe=False))

    live = [c for c in yesterday + today if c.engine_pick]
    assert len(live) == 3
    assert {c.symbol for c in live} == {"QQQ", "SPY", "MSFT"}
    assert sorted(c.pick_rank for c in picked) == [1, 2, 3]  # no duplicate rank=1


# --- The read-path guard -------------------------------------------------------
def test_a_pick_on_an_expired_row_stops_presenting_as_live() -> None:
    # Belt to the write-path braces: a scan that dies mid-cycle, or a row that
    # expires after being picked, must not keep claiming to be a commitment.
    rows = [_cand("dead", "NVDA", state=CandidateState.EXPIRED, picked=True, rank=1),
            _cand("live", "QQQ", picked=True, rank=1)]
    ranked = rank_candidates(rows, dedupe=False)
    by_id = {c.id: c for c in ranked}
    assert by_id["dead"].engine_pick is False
    assert by_id["live"].engine_pick is True


def test_a_rejected_pick_is_demoted_too() -> None:
    rows = [_cand("x", "AMZN", state=CandidateState.REJECTED, picked=True, rank=2)]
    assert rank_candidates(rows, dedupe=False)[0].engine_pick is False


def test_an_armed_pick_is_left_alone_by_the_read_guard() -> None:
    # Only TERMINAL states are demoted — an armed pick is exactly what should show.
    rows = [_cand("x", "QQQ", state=CandidateState.ARMED, picked=True, rank=1)]
    assert rank_candidates(rows, dedupe=False)[0].engine_pick is True
