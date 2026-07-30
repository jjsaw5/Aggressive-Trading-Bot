"""The two orders that should go in at entry: a target and a stop.

Built after a real loss. An INTC put spread was up 66% at a Wednesday close with
a profit limit resting at +109%; it never filled, the underlying gapped 10.5%
against the position overnight, and the trade finished at -80%. A $157 swing
decided entirely by where the target sat.

Two failures produced it, and this module addresses both:

1. **The visible target was the wrong one.** `risk.exit_plan` prices PT1/PT2 as a
   fraction of MAX PROFIT — correct for staged scale-outs when you hold size, but
   a much farther number than the fraction-of-DEBIT target that
   `RiskPlan.profit_target_pct` declares and that `analytics.policy_settlement`
   actually grades. Two targets, one dashboard, and the wider one got traded.
   The bracket prices BOTH sides off the debit, so what you work is what gets
   graded.

2. **Only the pleasant order was ever placed.** Profit limits go in at entry;
   stops don't, because placing a stop means conceding at entry that you might be
   wrong. Emitting them as one inseparable pair makes the decision once, while
   it is still cheap — before there is a position to have feelings about.

Sign convention matches the rest of the codebase: entry debit > 0, credit < 0.
Quoted prices come back POSITIVE, the way a broker ticket wants them, with the
closing action stated so the direction is never inferred.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

from app.domain.trades import TradePlan

# Both legs of the bracket are priced off the debit, deliberately symmetric with
# RiskPlan's own defaults so the order you work is the policy that gets graded.
DEFAULT_TARGET_PCT = 0.50
DEFAULT_STOP_PCT = 0.50

# A stop is a price, not a guarantee. Gaps are exactly how this module's
# originating loss happened, so the caveat travels WITH the number rather than
# living in documentation nobody reads mid-position.
GAP_CAVEAT = (
    "A stop is not gap protection — an overnight move opens through it and fills "
    "wherever the market is. The target is what protects a winner."
)


def _to_cent(x: float, *, up: bool) -> float:
    """Round to a tradeable cent, always in the direction that makes the order
    ACT SOONER — a target rounds toward an easier fill, a stop toward earlier
    protection. Half-cent ties decided by a coin flip is how a target ends up one
    tick out of reach, which is the failure this module exists to stop."""
    cents = x * 100.0
    r = math.ceil(cents - 1e-9) if up else math.floor(cents + 1e-9)
    return round(r / 100.0, 2)


class OrderBracket(BaseModel):
    """Target + stop as a matched pair, priced for a broker ticket."""

    entry_net: float  # signed: debit > 0, credit < 0
    contracts: int = Field(ge=1)
    is_credit: bool
    close_action: str  # sell_to_close | buy_to_close

    target_pct: float
    target_price: float  # positive net price to work
    target_pnl_usd: float

    stop_pct: float
    stop_price: float
    stop_pnl_usd: float

    # The staged scale-out target from risk.exit_plan, in the same units, so the
    # two are visibly different numbers instead of silently competing ones. None
    # when the structure has no defined max profit (e.g. a long single).
    scale_out_price: float | None = None
    scale_out_note: str = ""

    note: str = ""

    @property
    def paste(self) -> str:
        """One line you can read straight onto a broker ticket."""
        verb = "BUY" if self.is_credit else "SELL"
        out = (
            f"{verb} to close x{self.contracts} — "
            f"TARGET {self.target_price:.2f} ({self.target_pnl_usd:+.0f}) · "
            f"STOP {self.stop_price:.2f} ({self.stop_pnl_usd:+.0f})"
        )
        if self.scale_out_price is not None:
            out += f" · scale-out {self.scale_out_price:.2f}"
        return out


def bracket_from_entry(
    entry_net: float,
    contracts: int,
    *,
    target_pct: float = DEFAULT_TARGET_PCT,
    stop_pct: float = DEFAULT_STOP_PCT,
    width: float | None = None,
) -> OrderBracket | None:
    """Price the pair from what was paid or received.

    `width` (strike distance, per share) is optional and only used to show the
    staged scale-out target alongside — the number the dashboard shows elsewhere
    — so the difference between them is visible rather than surprising."""
    if entry_net == 0 or contracts < 1:
        return None
    is_credit = entry_net < 0
    basis = abs(entry_net)

    if is_credit:
        # Opened for a credit: close by buying it back. Profit = pay less.
        target_price = _to_cent(basis * (1.0 - target_pct), up=True)
        stop_price = _to_cent(basis * (1.0 + stop_pct), up=False)
        close_action = "buy_to_close"
    else:
        # Opened for a debit: close by selling. Profit = receive more.
        target_price = _to_cent(basis * (1.0 + target_pct), up=False)
        stop_price = _to_cent(basis * (1.0 - stop_pct), up=True)
        close_action = "sell_to_close"
    stop_price = max(0.01, stop_price)

    # Derived from the ROUNDED prices, not the ideal ones, so the dollars shown
    # are the dollars the posted order actually produces.
    sign = -1.0 if is_credit else 1.0
    target_pnl = sign * (target_price - basis) * 100.0 * contracts
    stop_pnl = sign * (stop_price - basis) * 100.0 * contracts

    scale_out = scale_note = None
    if width is not None and not is_credit:
        max_profit_ps = max(0.0, width - basis)
        if max_profit_ps > 0:
            scale_out = round(basis + 0.5 * max_profit_ps, 2)
            scale_note = (
                f"Staged scale-out at 50% of MAX profit ({scale_out:.2f}) — a farther "
                f"number than the target above, and only reachable on a large move. "
                f"Useful when you hold enough contracts to sell half; at x1 it is an "
                f"all-or-nothing bet that the target you can actually hit gets skipped."
            )

    return OrderBracket(
        entry_net=round(entry_net, 4),
        contracts=contracts,
        is_credit=is_credit,
        close_action=close_action,
        target_pct=target_pct,
        target_price=target_price,
        target_pnl_usd=round(target_pnl, 2),
        stop_pct=stop_pct,
        stop_price=stop_price,
        stop_pnl_usd=round(stop_pnl, 2),
        scale_out_price=scale_out,
        scale_out_note=scale_note or "",
        note=(
            f"Both prices are {int(target_pct * 100)}% / {int(stop_pct * 100)}% of the "
            f"{'credit' if is_credit else 'debit'} — the same basis the conviction "
            f"gate grades. Place them together, at entry. {GAP_CAVEAT}"
        ),
    )


def _plan_width(plan: TradePlan) -> float | None:
    """Strike distance for a two-leg vertical; None for anything else."""
    if len(plan.legs) != 2:
        return None
    a, b = plan.legs
    if a.option_type != b.option_type or a.expiration != b.expiration:
        return None
    w = abs(a.strike - b.strike)
    return w if w > 0 else None


def _pct(explicit: float | None, from_plan: float | None, fallback: float) -> float:
    """First real number wins. A recorded 0.0 means "no rule", not "exit at once",
    so it falls through to the default rather than pricing a degenerate order."""
    if explicit is not None:
        return explicit
    if from_plan:
        return from_plan
    return fallback


def bracket_from_plan(
    plan: TradePlan,
    *,
    target_pct: float | None = None,
    stop_pct: float | None = None,
) -> OrderBracket | None:
    """The bracket for a planned or open structure, honouring the plan's own exit
    percentages when it carries them (a DTE regime sets its own)."""
    risk = plan.risk
    return bracket_from_entry(
        plan.net_debit / 100.0,  # net_debit is per-1-lot, in dollars
        max(1, plan.contracts),
        target_pct=_pct(target_pct, risk.profit_target_pct if risk else None, DEFAULT_TARGET_PCT),
        stop_pct=_pct(stop_pct, risk.stop_loss_pct if risk else None, DEFAULT_STOP_PCT),
        width=_plan_width(plan),
    )
