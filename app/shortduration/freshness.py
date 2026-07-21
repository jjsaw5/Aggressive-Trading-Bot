"""Data-freshness policy.

A single 120s quote threshold is far too permissive for a trade-ready 0DTE
candidate. Freshness requirements tighten with the candidate's state and DTE track:
broad screening tolerates stale data, but an ARMED/OPEN 0DTE name needs a quote
that is seconds old. A candidate is not approvable when its quote is older than the
policy, the provider reports delayed data, the source is unknown, or the underlying
and option timestamps are materially inconsistent.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.config import settings
from app.domain.enums import CandidateState, DTECategory

# capability -> use_case -> config attribute (seconds)
_THRESHOLDS = {
    "underlying": {
        "broad": "freshness_broad_underlying_s",
        "watchlist": "freshness_watchlist_underlying_s",
        "armed": "freshness_armed_underlying_s",
        "open": "freshness_open_underlying_s",
    },
    "option": {
        "broad": "freshness_broad_option_s",
        "watchlist": "freshness_watchlist_option_s",
        "armed": "freshness_armed_option_s",
        "open": "freshness_open_option_s",
    },
    "internals": {"armed": "freshness_armed_internals_s", "open": "freshness_armed_internals_s"},
    "account": {"armed": "freshness_armed_account_s", "open": "freshness_armed_account_s"},
}

_STATE_USE_CASE = {
    CandidateState.DETECTED: "broad",
    CandidateState.EVALUATING: "broad",
    CandidateState.WATCHLIST: "watchlist",
    CandidateState.ARMED: "armed",
    CandidateState.TRIGGERED: "armed",
    CandidateState.PROPOSED: "armed",
    CandidateState.APPROVED: "armed",
    CandidateState.OPEN: "open",
    CandidateState.MANAGING: "open",
}


class FreshnessResult(BaseModel):
    ok: bool
    capability: str
    use_case: str
    threshold_s: float
    quote_age_s: float | None = None
    delayed: bool = False
    provider: str | None = None
    reason: str = ""


def use_case_for_state(state: CandidateState | None) -> str:
    return _STATE_USE_CASE.get(state, "broad") if state else "broad"


def threshold_seconds(capability: str, use_case: str, dte: DTECategory | None) -> float:
    """Resolve the freshness budget (seconds). Non-0DTE relaxes the trade-ready
    tiers to the watchlist budget — the sub-10s requirement is a 0DTE concern."""
    caps = _THRESHOLDS.get(capability, _THRESHOLDS["underlying"])
    uc = use_case
    if dte is not None and dte != DTECategory.ZERO_DTE and use_case in ("armed", "open"):
        uc = "watchlist"
    attr = caps.get(uc) or caps.get("watchlist") or caps.get("broad")
    return float(getattr(settings, attr))


def evaluate_quote_freshness(
    *, as_of: datetime | None, delayed_minutes: int | None, now: datetime,
    capability: str, state: CandidateState | None, dte: DTECategory | None,
    provider: str | None = None,
) -> FreshnessResult:
    use_case = use_case_for_state(state)
    threshold = threshold_seconds(capability, use_case, dte)
    if as_of is None:
        return FreshnessResult(ok=False, capability=capability, use_case=use_case,
                               threshold_s=threshold, provider=provider, reason="no quote available")
    age = round((now - as_of).total_seconds(), 1)
    delayed = bool(delayed_minutes and delayed_minutes > 0)
    if delayed:
        return FreshnessResult(ok=False, capability=capability, use_case=use_case, threshold_s=threshold,
                               quote_age_s=age, delayed=True, provider=provider,
                               reason="provider reports delayed data")
    if provider is None or provider == "unknown":
        return FreshnessResult(ok=False, capability=capability, use_case=use_case, threshold_s=threshold,
                               quote_age_s=age, provider=provider, reason="quote source unknown")
    if age > threshold:
        return FreshnessResult(ok=False, capability=capability, use_case=use_case, threshold_s=threshold,
                               quote_age_s=age, provider=provider,
                               reason=f"quote age {age:.0f}s exceeds {threshold:.0f}s budget for {use_case}")
    return FreshnessResult(ok=True, capability=capability, use_case=use_case, threshold_s=threshold,
                           quote_age_s=age, provider=provider, reason="fresh")


def timestamps_consistent(underlying_as_of: datetime | None, option_as_of: datetime | None,
                          *, max_skew_s: float = 60.0) -> bool:
    """Underlying and option quotes must not be materially inconsistent in time."""
    if underlying_as_of is None or option_as_of is None:
        return True  # can't compare; handled by per-quote checks
    return abs((underlying_as_of - option_as_of).total_seconds()) <= max_skew_s
