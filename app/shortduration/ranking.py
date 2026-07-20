"""Actionability ranking for the short-duration candidate board.

The board must lead with the highest-ranking, most *actionable* trade — not the
most recently scanned one. Ranking is a pure function of the candidate's state,
entry-gate status, score, and reward:risk, so it is deterministic and testable
without a DB or providers.

Buckets (best first):
    0 READY          — armed/triggered AND entry gates clear (tradeable now)
    1 ARMED_BLOCKED  — armed/triggered but gated (market closed, 0DTE cutoff, …)
    2 WATCHLIST      — scored above the watch threshold, not yet armed
    3 IN_FLIGHT      — proposed/approved/open/managing (already acted on)
    4 EVALUATING     — detected/evaluating, below the watch threshold
    5 TERMINAL       — rejected/expired/closed (collapsed at the bottom)

Within a bucket: score DESC → reward:risk DESC → most recent first.
"""

from __future__ import annotations

from app.domain.enums import CandidateState
from app.domain.shortduration import ShortDurationCandidate

READY = 0
ARMED_BLOCKED = 1
WATCHLIST = 2
IN_FLIGHT = 3
EVALUATING = 4
TERMINAL = 5

_ARMED = {CandidateState.ARMED, CandidateState.TRIGGERED}
_IN_FLIGHT = {
    CandidateState.PROPOSED, CandidateState.APPROVED,
    CandidateState.OPEN, CandidateState.MANAGING,
}
_TERMINAL = {CandidateState.REJECTED, CandidateState.EXPIRED, CandidateState.CLOSED}

_BUCKET_LABELS = {
    READY: "Ready to trade",
    ARMED_BLOCKED: "Armed — entry blocked",
    WATCHLIST: "Watchlist",
    IN_FLIGHT: "In flight",
    EVALUATING: "Evaluating",
    TERMINAL: "Rejected / closed",
}


def bucket(c: ShortDurationCandidate) -> int:
    """The actionability bucket for a candidate (lower = more actionable)."""
    if c.state in _ARMED:
        return READY if c.entry_allowed else ARMED_BLOCKED
    if c.state == CandidateState.WATCHLIST:
        return WATCHLIST
    if c.state in _IN_FLIGHT:
        return IN_FLIGHT
    if c.state in _TERMINAL:
        return TERMINAL
    return EVALUATING


def bucket_label(b: int) -> str:
    return _BUCKET_LABELS.get(b, "Other")


def _rank_key(c: ShortDurationCandidate) -> tuple:
    # Sort ascending: bucket first (0 best), then push high score / high R:R /
    # fresh to the top by negating them.
    ts = c.detected_at.timestamp() if c.detected_at else 0.0
    return (bucket(c), -(c.score or 0.0), -(c.reward_to_risk or 0.0), -ts)


def dedupe_latest(cands: list[ShortDurationCandidate]) -> list[ShortDurationCandidate]:
    """Collapse repeated scans of the same setup to the freshest candidate.

    Key is (symbol, strategy, dte_category) — re-running a scan produces a new
    row for the same setup; the newest reflects current state, so keep it."""
    latest: dict[tuple, ShortDurationCandidate] = {}
    for c in cands:
        # Distinct structures (long vs spread) of the same setup are separate
        # pickable plays, so the contract structure is part of the identity.
        structure = c.trade_plan.strategy if c.trade_plan else None
        key = (c.symbol, c.strategy, c.dte_category, structure)
        cur = latest.get(key)
        if cur is None or (c.detected_at and cur.detected_at and c.detected_at > cur.detected_at):
            latest[key] = c
    return list(latest.values())


def rank_candidates(
    cands: list[ShortDurationCandidate], *, dedupe: bool = True
) -> list[ShortDurationCandidate]:
    """Dedupe (optional) then order by actionability. Returns a new list."""
    rows = dedupe_latest(cands) if dedupe else list(cands)
    return sorted(rows, key=_rank_key)
