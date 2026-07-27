"""Staleness sweep: yesterday's candidates must never be served as live.

Regression for the Monday-morning board bug: a 0DTE candidate detected Friday
(legs expiring Friday) still rendered as an actionable watchlist row on Monday,
frozen quote-age badge and all — nothing ever retired pre-flight candidates.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.enums import CandidateState, DTECategory
from app.domain.shortduration import ContractRecommendation, ShortDurationCandidate
from app.shortduration.state import staleness_reason

_FRI = datetime(2026, 7, 24, 14, 30, tzinfo=UTC)
_MON = datetime(2026, 7, 27, 13, 40, tzinfo=UTC)


def _cand(*, state=CandidateState.WATCHLIST, dte=DTECategory.ZERO_DTE,
          detected_at=_FRI, exp: str | None = "2026-07-24") -> ShortDurationCandidate:
    contract = None
    if exp is not None:
        contract = ContractRecommendation(
            description="Put Debit Spread x1",
            legs=[{"option_type": "put", "strike": 230.0, "expiration": exp},
                  {"option_type": "put", "strike": 222.5, "expiration": exp}],
        )
    return ShortDurationCandidate(
        id="c1", symbol="AMZN", dte_category=dte, detected_at=detected_at,
        state=state, contract=contract,
    )


def test_expired_contract_is_stale_on_monday() -> None:
    why = staleness_reason(_cand(), now=_MON)
    assert why is not None and "2026-07-24" in why


def test_zero_dte_is_stale_next_day_even_without_contract() -> None:
    # A 0DTE setup is same-day by definition — its opening-range/VWAP levels
    # mean nothing tomorrow, whatever contract (if any) got attached.
    why = staleness_reason(_cand(exp=None), now=_MON)
    assert why is not None and "setup day" in why


def test_same_day_candidate_is_not_stale() -> None:
    fresh = _cand(detected_at=_MON, exp="2026-07-27")
    assert staleness_reason(fresh, now=_MON) is None


def test_short_dte_with_live_expiry_survives_the_weekend() -> None:
    # A 1-5DTE (or swing) candidate whose contract still exists is NOT swept.
    c = _cand(dte=DTECategory.SHORT_DTE, exp="2026-08-21")
    assert staleness_reason(c, now=_MON) is None


def test_in_flight_and_terminal_states_are_never_swept() -> None:
    for st in (CandidateState.OPEN, CandidateState.MANAGING,
               CandidateState.CLOSED, CandidateState.REJECTED):
        assert staleness_reason(_cand(state=st), now=_MON) is None


def test_listing_persists_the_expiry() -> None:
    # End to end through the repository: a stale Friday row comes back EXPIRED
    # from the board listing, and the transition is durably recorded.
    import uuid

    from app.db import repository

    c = _cand()
    c.id = uuid.uuid4().hex[:12]
    repository.save_short_duration_candidate(c)
    repository.list_short_duration_candidates(dte_category="0dte", limit=500)
    stored = repository.get_short_duration_candidate(c.id)
    assert stored is not None and stored.state == CandidateState.EXPIRED
    trail = repository.list_candidate_transitions(c.id)
    assert any(t.to_state == CandidateState.EXPIRED and "expired" in t.reason
               for t in trail)
