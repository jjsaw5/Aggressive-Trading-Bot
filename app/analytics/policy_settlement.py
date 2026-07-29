"""Grade a decision under the exit policy the app ACTUALLY runs.

Expiry settlement (see expiry_settlement.py) measures a hold-to-expiry payoff.
That is the right grade for probability-of-profit — which is itself a
hold-to-expiry claim — but it is the wrong grade for "would this strategy have
made money", because the app never holds to expiry: its plan takes profit at
40-60% of the debit, stops at -50%, and time-stops by DTE regime. A long-premium
book graded with no profit-taking looks far worse than the managed one, and a
short-premium book would look far better. Pooling the two would misrepresent
both.

This walks the structure's real daily marks from entry forward, applies the
decision's own recorded exit rules in order, and exits at the first trigger —
falling through to expiry intrinsic only if nothing fired.

Fidelity discipline: marks come from the historical options feed. When a leg's
series is unavailable the decision is ABSTAINED (None), never modelled into a
number — a policy grade built on a guessed path is worse than no grade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.analytics.expiry_settlement import plan_expiry, structure_value_at_expiry
from app.domain.enums import OptionAction, OptionType
from app.domain.outcomes import DecisionOutcome, DecisionSnapshot, OutcomeResult
from app.domain.trades import TradePlan
from app.logging_config import get_logger

log = get_logger(__name__)

SCRATCH_BAND_FRAC = 0.05


def occ_symbol(root: str, expiration: date, option_type: OptionType, strike: float) -> str:
    """OCC contract symbol, the identifier the historical feed keys on."""
    cp = "C" if option_type == OptionType.CALL else "P"
    return f"{root.upper()}{expiration:%y%m%d}{cp}{int(round(strike * 1000)):08d}"


@dataclass(frozen=True)
class PolicyExit:
    exit_date: date
    exit_net: float
    reason: str  # profit_target | stop_loss | time_stop | expiry


def _structure_net(plan: TradePlan, marks: dict[str, float]) -> float | None:
    """Signed net mark per share, or None if any leg is unpriced that day.

    Partial pricing is refused deliberately: a spread valued on one of two legs
    is not a cheap approximation, it is a different instrument."""
    total = 0.0
    for leg in plan.legs:
        sym = occ_symbol(leg.symbol, leg.expiration, leg.option_type, leg.strike)
        m = marks.get(sym)
        if m is None:
            return None
        long_leg = leg.action in (OptionAction.BUY_TO_OPEN, OptionAction.BUY_TO_CLOSE)
        total += m if long_leg else -m
    return round(total, 4)


def walk_policy(
    plan: TradePlan,
    entry_net: float,
    entry_date: date,
    expiry: date,
    marks_by_date: dict[date, dict[str, float]],
    *,
    underlying_close_at_expiry: float | None = None,
) -> PolicyExit | None:
    """Replay the plan's own exit rules forward from entry.

    Order within a day matters: the stop is checked BEFORE the profit target, so
    a day that traded through both is booked as a loss. Assuming the good fill on
    an ambiguous bar is how backtests flatter themselves."""
    risk = plan.risk
    target = risk.profit_target_pct
    stop = risk.stop_loss_pct
    time_stop = risk.time_stop_dte
    denom = abs(entry_net)
    if denom < 1e-6:
        return None

    for d in sorted(day for day in marks_by_date if entry_date < day <= expiry):
        net = _structure_net(plan, marks_by_date[d])
        if net is None:
            continue  # unpriced day — hold, don't invent a mark
        change = (net - entry_net) / denom
        if stop is not None and change <= -stop:
            return PolicyExit(d, net, "stop_loss")
        if target is not None and change >= target:
            return PolicyExit(d, net, "profit_target")
        if time_stop is not None and (expiry - d).days <= time_stop:
            return PolicyExit(d, net, "time_stop")

    if underlying_close_at_expiry is None:
        return None
    return PolicyExit(expiry, structure_value_at_expiry(plan, underlying_close_at_expiry), "expiry")


def settle_under_policy(
    snapshot: DecisionSnapshot,
    marks_by_date: dict[date, dict[str, float]],
    *,
    resolved_at: datetime | None = None,
    underlying_close_at_expiry: float | None = None,
    include_costs: bool = True,
) -> DecisionOutcome | None:
    """Grade one decision under its own exit plan. None when unpriceable."""
    plan = snapshot.trade_plan
    if plan is None or not plan.legs:
        return None
    expiry = snapshot.expiration or plan_expiry(plan)
    if expiry is None:
        return None
    resolved_at = resolved_at or datetime.now(UTC)

    entry_net = snapshot.entry_net_per_share
    contracts = max(1, snapshot.contracts)
    exit_ = walk_policy(
        plan, entry_net, snapshot.generated_at.date(), expiry, marks_by_date,
        underlying_close_at_expiry=underlying_close_at_expiry,
    )
    if exit_ is None:
        return None

    gross = round((exit_.exit_net - entry_net) * 100.0 * contracts, 2)
    costs = 0.0
    if include_costs:
        from app.analytics.outcomes import _resolution_costs
        from app.config import settings

        # A managed exit is a real trade, so unlike expiry settlement it crosses
        # a spread. Commission is counted here; the crossing itself is already in
        # the mid-to-mid mark series, so no synthetic slippage is added on top.
        costs = _resolution_costs(len(plan.legs), contracts, 0.0, settings)
    net = round(gross - costs, 2)

    max_loss = snapshot.max_loss_usd or abs(entry_net) * 100 * contracts
    band = abs(max_loss) * SCRATCH_BAND_FRAC
    result = (OutcomeResult.WIN if net > band
              else OutcomeResult.LOSS if net < -band
              else OutcomeResult.SCRATCH)

    return DecisionOutcome(
        decision_id=snapshot.decision_id,
        symbol=snapshot.symbol,
        horizon_label="managed_exit",
        resolved_at=resolved_at,
        elapsed_days=max(0, (exit_.exit_date - snapshot.generated_at.date()).days),
        result=result,
        realized_pnl_usd=net,
        realized_pnl_gross_usd=gross,
        costs_usd=round(costs, 2),
        used_bs_fallback=False,  # real marks only; unpriceable decisions abstain
        outcome_source="managed_policy",
        exit_reason=exit_.reason,
        note=(
            f"Exited {exit_.reason} on {exit_.exit_date} at {exit_.exit_net:+.2f}/sh "
            f"vs {entry_net:+.2f} entry, under the plan's own rules "
            f"(target {plan.risk.profit_target_pct:.0%}, stop {plan.risk.stop_loss_pct:.0%}, "
            f"time stop {plan.risk.time_stop_dte} DTE). This is the policy the app runs."
        ),
    )


async def load_marks(
    plan: TradePlan, start: date, end: date
) -> dict[date, dict[str, float]]:
    """Per-day marks for every leg, keyed by date then OCC symbol.

    Returns {} when the feed has no history provider — the caller must then
    abstain rather than fall back to a model."""
    from app.providers import registry

    try:
        hist = registry.historical_options_provider()
    except Exception as exc:  # noqa: BLE001 — no feed configured -> caller abstains
        log.warning("policy_marks_provider_unavailable", error=str(exc))
        return {}
    if hist is None:
        return {}
    out: dict[date, dict[str, float]] = {}
    for leg in plan.legs:
        sym = occ_symbol(leg.symbol, leg.expiration, leg.option_type, leg.strike)
        try:
            series = await hist.get_option_mark_series(sym, start, end)
        except Exception as exc:  # noqa: BLE001 — one leg failing abstains the decision
            log.warning("policy_marks_failed", option_symbol=sym, error=str(exc))
            continue
        for p in series:
            out.setdefault(p.ts.date(), {})[sym] = p.mark
    return out
