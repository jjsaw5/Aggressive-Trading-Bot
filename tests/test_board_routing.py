"""A board must mean what its name says, and a long option is not worthless.

Two defects, both of which made the boards lie about what was on them:

1. Candidates were filed under the SCAN that produced them. A daily-trend thesis
   is deliberately expressed weeks out (contracts.is_swing), so 65% of the
   "1-5DTE" board held 21-45 DTE contracts. The label meant nothing.

2. The rank key used `-(reward_to_risk or 0.0)`. A long single option has
   UNBOUNDED max profit, so its R:R is undefined — and `or 0.0` turned the best
   possible payoff profile into the worst possible sort position, burying every
   single leg beneath every spread. A quarter of everything scored is a single
   leg; essentially none of it ever reached a pick list.
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
    board_for,
    contract_horizon_days,
    mark_engine_picks,
    rank_candidates,
)

_NOW = datetime(2026, 7, 31, 14, 30, tzinfo=UTC)


def _legs(*exp_and_strikes) -> list[dict]:
    return [{"action": "buy_to_open", "option_type": "call", "strike": s,
             "expiration": e, "quantity": 1, "entry_price": 2.0}
            for e, s in exp_and_strikes]


def _cand(cid: str, symbol: str, *, dte=DTECategory.SHORT_DTE, exp="2026-08-05",
          legs=1, score=0.7, rr=None, state=CandidateState.WATCHLIST,
          detected=_NOW) -> ShortDurationCandidate:
    leg_specs = [(exp, 100.0)] + ([(exp, 105.0)] if legs == 2 else [])
    return ShortDurationCandidate(
        id=cid, symbol=symbol, dte_category=dte,
        strategy=ShortDurationStrategy.TREND_CONTINUATION, direction=Direction.BULLISH,
        detected_at=detected, regime=ShortDurationRegime.RANGE_BOUND,
        score=score, confidence=score, state=state, entry_allowed=True,
        reward_to_risk=rr,
        contract=ContractRecommendation(
            description=("Call Debit Spread x1" if legs == 2 else "Long Call x1"),
            legs=_legs(*leg_specs), max_loss_usd=150.0,
            max_profit_usd=350.0 if legs == 2 else None,
        ),
    )


# --- Horizon measurement ------------------------------------------------------
def test_horizon_is_measured_to_the_nearest_expiration() -> None:
    c = _cand("a", "SPY", exp="2026-08-21")
    assert contract_horizon_days(c) == 21


def test_a_candidate_with_no_structure_has_no_horizon() -> None:
    c = _cand("a", "SPY")
    c.contract = ContractRecommendation(description="")
    assert contract_horizon_days(c) is None


def test_an_unparseable_expiration_is_skipped_not_guessed() -> None:
    c = _cand("a", "SPY")
    c.contract.legs = [{"expiration": "not-a-date"}]
    assert contract_horizon_days(c) is None


# --- Board routing ------------------------------------------------------------
def test_a_true_short_dated_contract_stays_on_the_1_5dte_board() -> None:
    assert board_for(_cand("a", "SPY", exp="2026-08-05")) == DTECategory.SHORT_DTE  # 5d


def test_a_swing_horizon_contract_moves_to_medium() -> None:
    # THE regression: a 22-DTE contract is not a 1-5DTE trade, whatever scan
    # produced it.
    assert board_for(_cand("a", "NVDA", exp="2026-08-21")) == DTECategory.MEDIUM_DTE


def test_the_boundary_is_inclusive_at_five_days() -> None:
    assert board_for(_cand("a", "SPY", exp="2026-08-05")) == DTECategory.SHORT_DTE
    assert board_for(_cand("b", "SPY", exp="2026-08-06")) == DTECategory.MEDIUM_DTE


def test_zero_dte_is_never_rerouted() -> None:
    # 0DTE is same-day by definition; its contracts already match and its
    # staleness rules key off the category.
    c = _cand("a", "SPY", dte=DTECategory.ZERO_DTE, exp="2026-09-18")
    assert board_for(c) == DTECategory.ZERO_DTE


def test_a_candidate_without_a_structure_keeps_its_scan_board() -> None:
    # A rejected setup has nothing to route on — inventing a board would be worse
    # than leaving it where the scan filed it.
    c = _cand("a", "SPY")
    c.contract = ContractRecommendation(description="")
    assert board_for(c) == DTECategory.SHORT_DTE


# --- Single legs are no longer buried -----------------------------------------
def test_a_single_leg_outranks_a_lower_scoring_spread() -> None:
    # Previously impossible: the spread's R:R beat the single leg's None-as-zero
    # no matter how much better the single leg scored.
    single = _cand("s", "AAPL", legs=1, score=0.82, rr=None)
    spread = _cand("v", "MSFT", legs=2, score=0.61, rr=4.0)
    assert [c.id for c in rank_candidates([spread, single], dedupe=False)] == ["s", "v"]


def test_a_single_leg_can_become_an_engine_pick() -> None:
    single = _cand("s", "AAPL", legs=1, score=0.90, rr=None)
    spreads = [_cand(f"v{i}", f"SYM{i}", legs=2, score=0.70 - i * 0.01, rr=3.0)
               for i in range(3)]
    picked = mark_engine_picks(rank_candidates([*spreads, single], dedupe=False))
    assert picked[0].id == "s" and picked[0].pick_rank == 1


def test_reward_to_risk_no_longer_reorders_equal_scores() -> None:
    # R:R already feeds the composite via risk_quality; ranking on it again
    # double-counted it. At equal score, freshness decides.
    older = _cand("old", "A", rr=9.0, detected=_NOW - timedelta(minutes=30))
    newer = _cand("new", "B", rr=1.1, detected=_NOW)
    assert [c.id for c in rank_candidates([older, newer], dedupe=False)] == ["new", "old"]


def test_score_still_dominates_everything_below_it() -> None:
    lo = _cand("lo", "A", score=0.40, rr=9.0)
    hi = _cand("hi", "B", score=0.90, rr=None)
    assert [c.id for c in rank_candidates([lo, hi], dedupe=False)] == ["hi", "lo"]
