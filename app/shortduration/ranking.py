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

from datetime import date

from app.domain.enums import CandidateState, DTECategory
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
    # Sort ascending: bucket first (0 best), then score, then freshness.
    #
    # Reward:risk is deliberately NOT a sort term. It is already a scoring
    # component (risk_quality), so ranking on it again double-counts it — and the
    # `or 0.0` fallback it needed was actively wrong: a long single option has
    # UNBOUNDED max profit, so its R:R is undefined, not zero. Treating undefined
    # as zero buried every single-leg candidate beneath every spread at equal
    # score. A quarter of everything the scanner produces is a single leg, and
    # essentially none of it ever reached a pick list.
    ts = c.detected_at.timestamp() if c.detected_at else 0.0
    return (bucket(c), -(c.score or 0.0), -ts)


# How many setups the engine commits to per board per scan. Small on purpose: a
# pick list long enough to include everything is not a prediction.
ENGINE_PICK_LIMIT = 3

# Blocks that are about WHEN, not about whether the setup is any good.
_TIMING_ONLY_BLOCKS = {"time_of_day_blocked", "RejectReason.TIME_OF_DAY_BLOCKED"}


def mark_engine_picks(
    ranked: list[ShortDurationCandidate], *, limit: int = ENGINE_PICK_LIMIT
) -> list[ShortDurationCandidate]:
    """Go on record with the setups the engine would actually take.

    Takes an ALREADY board-ranked list and flags the top few that are genuinely
    actionable — a sized defined-risk structure exists and the entry gates are
    clear. Everything else stays context. Returns the flagged candidates so the
    caller can persist them.

    This is a recorded prediction, not advice: conviction is UNCALIBRATED (see
    the conviction gate). Its whole purpose is to be graded later — without a
    committed pick list there is nothing to score the engine against."""
    eligible = [
        c for c in ranked
        if bucket(c) != TERMINAL
        and c.contract is not None
        and c.contract.legs
        and _pickable_gates(c)
    ]
    # One pick per underlying: the same symbol usually offers both a long leg and
    # a vertical, and spending two of three slots on one name is a weaker
    # commitment than naming three different setups.
    picked: list[ShortDurationCandidate] = []
    seen: set[str] = set()
    for c in eligible:
        if len(picked) >= limit:
            break
        if c.symbol in seen:
            continue
        seen.add(c.symbol)
        rank = len(picked) + 1
        c.engine_pick = True
        c.pick_rank = rank
        gate = "" if c.entry_allowed else " Entry is gated until the session opens."
        c.pick_reason = (
            f"Engine pick #{rank} on the {c.dte_category.value} board: top-ranked setup "
            f"with a sized defined-risk structure and no risk-gate block.{gate} "
            "UNCALIBRATED — recorded so it can be graded, not a recommendation."
        )
        picked.append(c)
    return picked



def _pickable_gates(c: ShortDurationCandidate) -> bool:
    """Timing gates don't disqualify a pick; risk gates do.

    "Market is closed" says nothing about whether the engine likes the setup —
    blocking on it would leave the pick list empty exactly when you're planning
    the next session. A portfolio/daily-loss block is different: the app's own
    risk rules refusing the trade means it would not take it."""
    if c.entry_allowed:
        return True
    reasons = {str(r) for r in (c.reject_reasons or [])}
    if not reasons:
        return True  # gated with no stated reason — don't infer a risk veto
    return reasons <= _TIMING_ONLY_BLOCKS


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


# A contract expiring beyond this many days is not a 1-5DTE trade, whatever the
# scan that produced it was called.
SHORT_DTE_MAX_DAYS = 5


def contract_horizon_days(c: ShortDurationCandidate) -> int | None:
    """Days from detection to the structure's NEAREST expiration, or None when the
    candidate carries no dated structure (a rejected setup has nothing to file)."""
    legs = (c.contract.legs if c.contract else None) or []
    exps: list[date] = []
    for lg in legs:
        raw = lg.get("expiration") if isinstance(lg, dict) else getattr(lg, "expiration", None)
        if raw is None:
            continue
        try:
            exps.append(raw if isinstance(raw, date) else date.fromisoformat(str(raw)))
        except (TypeError, ValueError):
            continue
    if not exps or c.detected_at is None:
        return None
    return (min(exps) - c.detected_at.date()).days


def board_for(c: ShortDurationCandidate) -> DTECategory:
    """Which board a candidate belongs on, by the horizon it can actually be
    traded at rather than the scan that produced it.

    0DTE stays 0DTE — it is same-day by definition and its contracts already
    match. A 1-5DTE scan, though, deliberately expresses a daily-trend thesis
    weeks out (contracts.is_swing), and those were filed on the 1-5DTE board
    where they became 65% of it. Routing on the contract keeps the label honest:
    "1-5DTE" means a contract expiring within five days, and nothing else."""
    if c.dte_category == DTECategory.ZERO_DTE:
        return DTECategory.ZERO_DTE
    days = contract_horizon_days(c)
    if days is None:
        return c.dte_category  # no structure to route on — leave the scan's own board
    return DTECategory.SHORT_DTE if days <= SHORT_DTE_MAX_DAYS else DTECategory.MEDIUM_DTE


def retire_engine_picks(
    cands: list[ShortDurationCandidate], *, keep_ids: set[str] | None = None
) -> list[ShortDurationCandidate]:
    """Clear the pick flag on prior commitments. Returns only what changed.

    A pick list is a commitment to NOW — "these are the setups the engine would
    take this scan". The flag is persisted per row and, until this existed,
    nothing ever cleared it: every scan added up to three more permanently-marked
    rows, so the board accumulated picks from every scan ever run. Two days of
    that and a bullish tape reads as uniformly bearish, because yesterday's
    bearish commitments never retired.

    Retiring is deliberately unconditional rather than age-based: exactly one
    scan's worth of picks may be live, and the newest scan owns them."""
    keep = keep_ids or set()
    changed: list[ShortDurationCandidate] = []
    for c in cands:
        if c.engine_pick and c.id not in keep:
            c.engine_pick = False
            c.pick_rank = None
            c.pick_reason = ""
            changed.append(c)
    return changed


def _demote_dead_picks(cands: list[ShortDurationCandidate]) -> None:
    """A pick on a rejected/expired/closed row is not a live commitment.

    Read-path belt to the write-path braces: a scan that dies between marking and
    retiring, or a row that expires after being picked, must not keep presenting
    itself as something the engine would take right now."""
    for c in cands:
        if c.engine_pick and bucket(c) == TERMINAL:
            c.engine_pick = False
            c.pick_rank = None


def rank_candidates(
    cands: list[ShortDurationCandidate], *, dedupe: bool = True
) -> list[ShortDurationCandidate]:
    """Dedupe (optional) then order by actionability. Returns a new list."""
    rows = dedupe_latest(cands) if dedupe else list(cands)
    _demote_dead_picks(rows)
    return sorted(rows, key=_rank_key)
