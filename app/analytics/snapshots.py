"""Build a `DecisionSnapshot` from an actionable candidate.

Pure translation: it reads the prediction and frozen market state already
attached to a candidate (analytics + the volatility signal) and packs them into
a stable warehouse record. No I/O, no recomputation — so the snapshot reflects
exactly what the engine believed at scan time.
"""

from __future__ import annotations

from app.domain.candidates import TradeCandidate
from app.domain.outcomes import DecisionSnapshot, DecisionSource


def _iv_from_signals(candidate: TradeCandidate) -> tuple[float | None, float | None]:
    """(iv30, iv_rank) pulled from the volatility signal details, if present."""
    vol = next((s for s in candidate.signals if s.name == "volatility"), None)
    if vol is None:
        return None, None
    iv30 = vol.details.get("iv30")
    iv_rank = vol.details.get("iv_rank")
    return (
        float(iv30) if isinstance(iv30, (int, float)) else None,
        float(iv_rank) if isinstance(iv_rank, (int, float)) else None,
    )


def snapshot_from_candidate(
    candidate: TradeCandidate, *, source: DecisionSource = DecisionSource.SCAN
) -> DecisionSnapshot | None:
    """Return a frozen decision record, or None if the candidate is not actionable."""
    plan = candidate.trade_plan
    if not candidate.is_actionable or plan is None:
        return None

    analytics = plan.analytics
    exit_plan = plan.exit_plan
    iv30, iv_rank = _iv_from_signals(candidate)

    # Entry spot: frozen on the analytics at compute time.
    entry_spot = (analytics.spot_at_analysis if analytics else None) or 0.0

    # Per-share signed net (debit > 0, credit < 0); the exit plan already carries
    # it, otherwise fall back to net_debit expressed per share.
    entry_net = (
        exit_plan.entry_net_per_share
        if exit_plan is not None
        else round(plan.net_debit / 100.0, 4)
    )

    # Nearest expiration drives the DTE-at-entry.
    expiration = min((leg.expiration for leg in plan.legs), default=None)
    dte = (expiration - candidate.generated_at.date()).days if expiration else None

    decision_id = f"{candidate.scan_id}:{candidate.symbol.upper()}"

    return DecisionSnapshot(
        decision_id=decision_id,
        scan_id=candidate.scan_id,
        symbol=candidate.symbol.upper(),
        source=source,
        direction=plan.direction,
        strategy=plan.strategy,
        generated_at=candidate.generated_at,
        composite_score=candidate.composite_score,
        probability_of_profit=(analytics.probability_of_profit if analytics else None),
        reward_to_risk=plan.risk.reward_to_risk,
        expected_value_usd=(analytics.expected_value_usd if analytics else None),
        breakevens=list(analytics.breakevens) if analytics else [],
        is_credit=bool(analytics.is_credit) if analytics else (entry_net < 0),
        entry_spot=round(entry_spot, 4),
        entry_iv=iv30,
        iv_rank=iv_rank,
        entry_net_per_share=entry_net,
        max_profit_usd=plan.risk.max_profit_usd,
        max_loss_usd=plan.risk.max_loss_usd,
        contracts=plan.contracts,
        expiration=expiration,
        dte_at_entry=dte,
        trade_plan=plan,
    )
