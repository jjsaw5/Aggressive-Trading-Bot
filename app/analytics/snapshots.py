"""Build a `DecisionSnapshot` from an actionable candidate.

Pure translation: it reads the prediction and frozen market state already
attached to a candidate (analytics + the volatility signal) and packs them into
a stable warehouse record. No I/O, no recomputation — so the snapshot reflects
exactly what the engine believed at scan time.

Two lineages produce snapshots:
- the funnel (`snapshot_from_candidate`, TradeCandidate) — legacy analytics POP;
- the short-duration scanner (`snapshot_from_short_duration`) — POP from the
  traded-expiry IV. Each stamps its `pop_source` and scoring-model version so the
  calibration harness grades the constructs separately and the pre-v3 filter
  applies (see app/analytics/calibration.py).
"""

from __future__ import annotations

from app.domain.candidates import TradeCandidate
from app.domain.outcomes import DecisionSnapshot, DecisionSource

# POP methodology label for short-duration decisions: Black-Scholes zero-drift
# N(d2) priced on the IV of the TRADED expiry (post horizon-fix). See
# detection._candidate_odds for the derivation + card provenance.
SD_POP_SOURCE = "bs_zero_drift_traded_expiry_iv"


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


def _flow_quality_from_signals(candidate: TradeCandidate) -> float | None:
    """The sibling scanner's proprietary flow-quality, frozen from the flow
    signal's details. Shadow only — recorded, never scored."""
    flow = next((s for s in candidate.signals if s.name == "options_flow"), None)
    if flow is None:
        return None
    q = flow.details.get("flow_quality_proprietary")
    return float(q) if isinstance(q, (int, float)) and not isinstance(q, bool) else None


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
    flow_quality = _flow_quality_from_signals(candidate)

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
        flow_quality_proprietary=flow_quality,
        entry_net_per_share=entry_net,
        max_profit_usd=plan.risk.max_profit_usd,
        max_loss_usd=plan.risk.max_loss_usd,
        contracts=plan.contracts,
        expiration=expiration,
        dte_at_entry=dte,
        trade_plan=plan,
    )


def snapshot_from_live_trade(trade) -> DecisionSnapshot | None:
    """Freeze a REAL (imported/synced) position into the decision warehouse so the
    calibration scorecard can grade live trades alongside scan/paper decisions.

    A live import carries no engine prediction: composite_score is 0.0 as a
    required-field placeholder and the calibration harness EXCLUDES live-source
    decisions from the score buckets (there is no engine score to grade) — live
    trades contribute realized win-rate / P&L under their own source group."""
    plan = trade.trade_plan
    if plan is None:
        return None
    from app.quant.analytics import structure_breakevens

    expiration = min((leg.expiration for leg in plan.legs), default=None)
    dte = (expiration - trade.opened_at.date()).days if expiration else None
    return DecisionSnapshot(
        decision_id=f"live:{trade.id}",
        scan_id=f"live:{trade.id}",
        symbol=trade.symbol.upper(),
        source=DecisionSource.LIVE,
        direction=plan.direction,
        strategy=plan.strategy,
        generated_at=trade.opened_at,
        composite_score=0.0,  # no engine prediction — excluded from score buckets
        probability_of_profit=None,
        reward_to_risk=plan.risk.reward_to_risk if plan.risk else None,
        breakevens=structure_breakevens(plan),
        is_credit=plan.net_debit < 0,
        entry_spot=0.0,  # not recorded at import time; outcome carries the exit truth
        entry_net_per_share=round(plan.net_debit / 100.0, 4),
        max_profit_usd=plan.risk.max_profit_usd if plan.risk else None,
        max_loss_usd=plan.risk.max_loss_usd if plan.risk else 0.0,
        contracts=plan.contracts,
        expiration=expiration,
        dte_at_entry=dte,
        trade_plan=plan,
    )


def snapshot_from_short_duration(cand) -> DecisionSnapshot | None:
    """Freeze a short-duration candidate into the decision warehouse.

    Only rankable decisions are warehoused: a tradeable plan must exist and the
    scorecard must NOT be abstained (an abstained rank is not a decision — its
    inputs were insufficient to read). POP here is the traded-expiry-IV construct;
    the snapshot stamps `pop_source` and the scoring-model version so calibration
    grades it per construct and the pre-v3 hard-filter applies."""
    from app.config import get_settings
    from app.quant.analytics import structure_breakevens

    plan = cand.trade_plan
    if plan is None:
        return None
    sc = cand.scorecard
    if sc is not None and sc.abstained:
        return None

    expiration = min((leg.expiration for leg in plan.legs), default=None)
    dte = (expiration - cand.detected_at.date()).days if expiration else None
    analytics = plan.analytics
    entry_spot = (analytics.spot_at_analysis if analytics else None) or 0.0
    entry_net = (
        plan.exit_plan.entry_net_per_share
        if plan.exit_plan is not None
        else round(plan.net_debit / 100.0, 4)
    )
    return DecisionSnapshot(
        decision_id=f"sd:{cand.id}",
        scan_id=f"sd:{cand.id}",
        symbol=cand.symbol.upper(),
        source=DecisionSource.SCAN,
        direction=cand.direction,
        strategy=plan.strategy,
        generated_at=cand.detected_at,
        composite_score=cand.score,
        probability_of_profit=cand.probability_of_profit,
        pop_source=SD_POP_SOURCE if cand.probability_of_profit is not None else "",
        reward_to_risk=plan.risk.reward_to_risk if plan.risk else None,
        breakevens=structure_breakevens(plan),
        is_credit=plan.net_debit < 0,
        entry_spot=round(entry_spot, 4),
        entry_net_per_share=entry_net,
        max_profit_usd=plan.risk.max_profit_usd if plan.risk else None,
        max_loss_usd=plan.risk.max_loss_usd if plan.risk else 0.0,
        contracts=plan.contracts,
        expiration=expiration,
        dte_at_entry=dte,
        scoring_model_version=get_settings().scoring_model_version,
        trade_plan=plan,
    )
