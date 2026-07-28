"""Settle a recorded decision at ITS OWN expiry, from the underlying's close.

Why this and not "resolve against the current price": the warehouse holds
thousands of decisions made on different days with different expiries. Marking
an April 0DTE decision against today's tape would produce a confident number
that means nothing. Each decision has to be graded at the horizon it was made
for.

Why the underlying close is EXACT here, not a proxy: at expiration a
defined-risk option structure has no extrinsic value left, so its settlement
value is fully determined by where the underlying finished. Summing signed
intrinsic across the legs IS the payoff — no option marks required, and no
modelling assumption smuggled in.

Why this is the right grade for POP specifically: the app's probability of
profit is a hold-to-expiry quantity (P(finish past break-even at expiry)).
Settling at expiry measures exactly that claim, so prediction and outcome are
finally the same question.

What it does NOT measure: the app's real exit discipline takes profit at
40-60% and stops at -50% (see dte_regime), so a hold-to-expiry P&L is not what
the managed trade would have returned. Outcomes are stamped
`outcome_source="expiry_settlement"` and `horizon_label="at_expiry"` so this
policy is never silently pooled with traded closes.
"""

from __future__ import annotations

from datetime import date, datetime

from app.domain.enums import Direction, OptionAction, OptionType
from app.domain.outcomes import DecisionOutcome, DecisionSnapshot, OutcomeResult
from app.domain.trades import TradePlan

# P&L inside this fraction of the structure's own max loss counts as a scratch
# rather than a win/loss — a $2 result on a $250 risk is noise, not a signal.
SCRATCH_BAND_FRAC = 0.05


def structure_value_at_expiry(plan: TradePlan, underlying_close: float) -> float:
    """Signed net value per share of the structure at expiration.

    Long legs contribute their intrinsic value, short legs subtract it. Same
    sign convention as entry: a debit structure settles positive when it
    finishes in the money."""
    total = 0.0
    for leg in plan.legs:
        if leg.option_type == OptionType.CALL:
            intrinsic = max(0.0, underlying_close - leg.strike)
        else:
            intrinsic = max(0.0, leg.strike - underlying_close)
        long_leg = leg.action in (OptionAction.BUY_TO_OPEN, OptionAction.BUY_TO_CLOSE)
        total += intrinsic if long_leg else -intrinsic
    return round(total, 4)


def plan_expiry(plan: TradePlan) -> date | None:
    """The date the structure resolves — its nearest leg expiration."""
    exps = [lg.expiration for lg in plan.legs]
    return min(exps) if exps else None


def settle_at_expiry(
    snapshot: DecisionSnapshot,
    underlying_close: float,
    *,
    resolved_at: datetime,
    include_costs: bool = True,
) -> DecisionOutcome | None:
    """Grade one decision at its own expiry. None when it can't be settled."""
    plan = snapshot.trade_plan
    if plan is None or not plan.legs:
        return None
    expiry = snapshot.expiration or plan_expiry(plan)
    if expiry is None:
        return None

    # Prefer the snapshot's own frozen economics — that is the record of what was
    # actually decided; the plan is kept for replay.
    contracts = max(1, snapshot.contracts)
    entry_net = snapshot.entry_net_per_share  # debit > 0 / credit < 0
    settle_net = structure_value_at_expiry(plan, underlying_close)
    gross = round((settle_net - entry_net) * 100.0 * contracts, 2)

    costs = 0.0
    if include_costs:
        from app.analytics.outcomes import _resolution_costs
        from app.config import settings

        # Commission only: there is no bid/ask left to cross at settlement.
        costs = _resolution_costs(len(plan.legs), contracts, 0.0, settings)
    net = round(gross - costs, 2)

    # Scratch band scaled to the structure's own risk, so "roughly flat" doesn't
    # get graded as a win on a big position or a loss on a tiny one.
    max_loss = snapshot.max_loss_usd or abs(entry_net) * 100 * contracts
    band = abs(max_loss) * SCRATCH_BAND_FRAC
    if net > band:
        result = OutcomeResult.WIN
    elif net < -band:
        result = OutcomeResult.LOSS
    else:
        result = OutcomeResult.SCRATCH

    ret_pct = None
    direction_ok = None
    if snapshot.entry_spot:
        ret_pct = round((underlying_close - snapshot.entry_spot) / snapshot.entry_spot * 100.0, 4)
        if snapshot.direction in (Direction.BULLISH, Direction.BEARISH):
            direction_ok = (ret_pct > 0) if snapshot.direction == Direction.BULLISH else (ret_pct < 0)

    return DecisionOutcome(
        decision_id=snapshot.decision_id,
        symbol=snapshot.symbol,
        horizon_label="at_expiry",
        resolved_at=resolved_at,
        elapsed_days=max(0, (expiry - snapshot.generated_at.date()).days),
        spot_at_resolution=round(underlying_close, 4),
        underlying_return_pct=ret_pct,
        direction_correct=direction_ok,
        result=result,
        realized_pnl_usd=net,
        realized_pnl_gross_usd=gross,
        costs_usd=round(costs, 2),
        used_bs_fallback=False,  # settlement is exact intrinsic; nothing modelled
        outcome_source="expiry_settlement",
        note=(
            f"Settled at expiry {expiry} on the underlying close "
            f"({underlying_close:g}); structure worth {settle_net:+.2f}/sh vs "
            f"{entry_net:+.2f} entry. Hold-to-expiry policy — NOT the managed "
            "exit plan (targets/stops would have exited earlier)."
        ),
    )
