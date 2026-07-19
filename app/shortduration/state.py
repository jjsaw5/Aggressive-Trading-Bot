"""Short-duration candidate state machine.

Enforces the legal lifecycle and records every transition (previous/new state,
timestamp, trigger, actor, reason, score-at-transition) for a full audit trail.
Illegal transitions raise rather than silently corrupting state. Live execution
is NOT reachable here — APPROVED→OPEN is a research/paper transition; real orders
still go through the existing ExecutionGuard.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.enums import CandidateState as S
from app.domain.shortduration import CandidateTransition, ShortDurationCandidate

_TERMINAL = {S.CLOSED, S.REJECTED, S.EXPIRED}

# Legal forward transitions. REJECTED/EXPIRED are reachable from any non-terminal
# state (added below).
_LEGAL: dict[S, set[S]] = {
    S.DETECTED: {S.EVALUATING},
    S.EVALUATING: {S.WATCHLIST, S.ARMED},
    S.WATCHLIST: {S.ARMED, S.EVALUATING},
    S.ARMED: {S.TRIGGERED, S.WATCHLIST},
    # TRIGGERED -> OPEN is the PAPER/research path (no human approval needed).
    # The human-approved LIVE path is TRIGGERED -> PROPOSED -> APPROVED -> OPEN,
    # and any live order still passes the ExecutionGuard.
    S.TRIGGERED: {S.PROPOSED, S.OPEN},
    S.PROPOSED: {S.APPROVED},
    S.APPROVED: {S.OPEN},
    S.OPEN: {S.MANAGING, S.CLOSED},
    S.MANAGING: {S.CLOSED},
}
for _s in list(_LEGAL):
    _LEGAL[_s] |= {S.REJECTED, S.EXPIRED}


def can_transition(frm: S, to: S) -> bool:
    if frm in _TERMINAL:
        return False
    return to in _LEGAL.get(frm, set())


def transition(
    candidate: ShortDurationCandidate,
    to: S,
    *,
    trigger: str,
    actor: str = "system",
    reason: str = "",
    at: datetime | None = None,
) -> CandidateTransition:
    """Mutate the candidate to `to` and return the audit record (caller persists
    both). Raises ValueError on an illegal transition."""
    frm = candidate.state
    if frm == to:
        raise ValueError(f"{candidate.id} is already {to.value}")
    if not can_transition(frm, to):
        raise ValueError(f"Illegal transition {frm.value} -> {to.value}")
    at = at or datetime.now(UTC)
    candidate.state = to
    return CandidateTransition(
        candidate_id=candidate.id, from_state=frm, to_state=to, at=at,
        trigger=trigger, actor=actor, reason=reason, score_at=candidate.score,
    )


def classify_initial_state(score: float, *, watchlist_at: float, arm_at: float) -> S:
    """Map a fresh detection's score to its starting state past EVALUATING."""
    if score >= arm_at:
        return S.ARMED
    if score >= watchlist_at:
        return S.WATCHLIST
    return S.EVALUATING
