"""Short-duration candidate state machine.

Enforces the legal lifecycle and records every transition (previous/new state,
timestamp, trigger, actor, reason, score-at-transition) for a full audit trail.
Illegal transitions raise rather than silently corrupting state. Live execution
is NOT reachable here — APPROVED→OPEN is a research/paper transition; real orders
still go through the existing ExecutionGuard.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.domain.enums import CandidateState as S
from app.domain.enums import DTECategory
from app.domain.shortduration import CandidateTransition, ShortDurationCandidate

_TERMINAL = {S.CLOSED, S.REJECTED, S.EXPIRED}
_ET = ZoneInfo("America/New_York")
# Pre-flight states the staleness sweep may expire. OPEN/MANAGING are a live
# paper position — the position monitor settles those at expiry; terminal
# states are already dead. PROPOSED/APPROVED are human-touched, so they are
# swept ONLY by the contract-expired rule (an expired contract is physically
# unexecutable), never by the softer 0DTE-day rule.
_SWEEPABLE = {S.DETECTED, S.EVALUATING, S.WATCHLIST, S.ARMED, S.TRIGGERED}
_SWEEPABLE_ON_EXPIRY = _SWEEPABLE | {S.PROPOSED, S.APPROVED}


def staleness_reason(
    c: ShortDurationCandidate, *, now: datetime | None = None
) -> str | None:
    """Why this candidate's setup is dead, or None while it is still current.

    A candidate must never render as actionable once (a) any recommended
    contract's expiration has passed, or (b) for 0DTE, its detection day (ET) is
    over — a 0DTE setup is same-day by definition, and its entry levels (opening
    range, VWAP) mean nothing the next morning."""
    if c.state not in _SWEEPABLE_ON_EXPIRY:
        return None
    now = now or datetime.now(UTC)
    today_et = now.astimezone(_ET).date()

    exps: list[date] = []
    if c.trade_plan is not None:
        exps.extend(lg.expiration for lg in c.trade_plan.legs)
    elif c.contract is not None:
        for lg in c.contract.legs:
            try:
                exps.append(date.fromisoformat(str(lg.get("expiration"))))
            except (TypeError, ValueError):
                continue
    if exps and min(exps) < today_et:
        return f"contract expired {min(exps).isoformat()}"

    if c.state not in _SWEEPABLE:
        return None
    if c.dte_category == DTECategory.ZERO_DTE and c.detected_at is not None:
        detected_day = c.detected_at.astimezone(_ET).date()
        if detected_day < today_et:
            return f"0DTE setup day {detected_day.isoformat()} is over"
    return None

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


def classify_initial_state(
    score: float, *, watchlist_at: float, arm_at: float, allow_arm: bool = True
) -> S:
    """Map a fresh detection's score to its starting state past EVALUATING.

    `allow_arm=False` (Layer-1 arming discipline) caps an arm-worthy score at
    WATCHLIST — the hand-weighted rank is trusted enough to watch, not to arm,
    when probability-of-profit is uncomputable or conviction is disallowed for the
    track (e.g. 0DTE). The score still has to clear the watchlist bar to surface."""
    if score >= arm_at:
        return S.ARMED if allow_arm else S.WATCHLIST
    if score >= watchlist_at:
        return S.WATCHLIST
    return S.EVALUATING


# Linear progression used to auto-advance a candidate to a target state.
_LINEAR = [
    S.DETECTED, S.EVALUATING, S.WATCHLIST, S.ARMED, S.TRIGGERED,
    S.PROPOSED, S.APPROVED, S.OPEN, S.MANAGING, S.CLOSED,
]


def advance(
    candidate: ShortDurationCandidate,
    target: S,
    *,
    trigger: str,
    actor: str = "system",
    reason: str = "",
    at: datetime | None = None,
) -> list[CandidateTransition]:
    """Walk the legal forward path from the candidate's current state to
    `target`, recording each step. Raises ValueError if `target` is behind the
    current state or unreachable by legal forward transitions."""
    at = at or datetime.now(UTC)
    if candidate.state == target:
        return []
    if candidate.state not in _LINEAR or _LINEAR.index(candidate.state) > _LINEAR.index(target):
        raise ValueError(f"Cannot advance {candidate.state.value} -> {target.value}")
    trail: list[CandidateTransition] = []
    # At each hop, jump straight to the target if that's legal (so the paper path
    # TRIGGERED -> OPEN is taken directly, not via PROPOSED/APPROVED); otherwise
    # take the first legal forward step that doesn't overshoot the target.
    while candidate.state != target:
        if can_transition(candidate.state, target):
            nxt = target
        else:
            idx = _LINEAR.index(candidate.state)
            nxt = next(
                (s for s in _LINEAR[idx + 1:]
                 if can_transition(candidate.state, s) and _LINEAR.index(s) < _LINEAR.index(target)),
                None,
            )
        if nxt is None:
            raise ValueError(f"No legal path {candidate.state.value} -> {target.value}")
        trail.append(transition(candidate, nxt, trigger=trigger, actor=actor, reason=reason, at=at))
    return trail
