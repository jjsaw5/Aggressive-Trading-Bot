"""What the trade would have made had the fills been worse (item 2.4).

The pre-registration makes this the headline number, not a footnote.
`CAPTURE_WINDOW_PREREGISTRATION.md` §5.4 requires expectancy computed three
ways — at stored mid, at one tick worse on entry and exit, and at half-spread
worse — and §6 makes **H4 passing after the one-tick stress** a precondition for
live capital. A mid-to-mid P&L is the optimistic bound of the three and the only
one the audited corpus ever recorded.

Why this matters at this account size is arithmetic, not theory. A one-cent tick
on each side of a two-leg spread is $4 per contract round trip. Against a $100
defined-risk cap that is 4% of max loss surrendered to the tick alone, before any
spread. An edge smaller than that is not an edge you can collect.

SOURCE LABELLING IS MANDATORY. A stress computed from an execution-derived
spread and one computed from a quoted NBBO are different measurements, and
pooling them would make the H4 figure unreadable. Every result carries
`cost_stress_source`, and a stress is never produced without one.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.trades import TradePlan

# One cent, the minimum increment on the contracts this app trades. Robinhood
# reports `min_ticks.above_tick = 0.01` on the chains checked; a penny-pilot name
# with a different increment would need this per contract, not per app.
TICK = 0.01

SOURCE_EFFECTIVE = "effective_from_side_volume"
SOURCE_NBBO = "nbbo"


@dataclass(frozen=True)
class CostStress:
    """P&L under progressively worse fill assumptions, all in dollars."""

    pnl_mid_usd: float
    pnl_1tick_usd: float
    pnl_half_spread_usd: float | None
    source: str
    legs: int
    contracts: int

    @property
    def tick_drag_usd(self) -> float:
        """What the one-tick assumption alone costs. The number to compare
        against a claimed edge."""
        return round(self.pnl_mid_usd - self.pnl_1tick_usd, 2)


def _round_trip_ticks(legs: int, contracts: int) -> float:
    """Dollars given up crossing one tick per leg, on the way in AND out.

    Both directions count: you pay it entering and again exiting. Halving it is
    the most common way a backtest understates cost.
    """
    return legs * 2 * TICK * 100.0 * contracts


def stress_pnl(
    plan: TradePlan,
    pnl_mid_usd: float,
    contracts: int,
    *,
    half_spread_per_leg: float | None = None,
    source: str = SOURCE_EFFECTIVE,
) -> CostStress:
    """Restate a mid-based P&L under worse fills.

    `half_spread_per_leg` is per share, per leg — from `OptionMinuteBar
    .effective_spread` (executions) or a quoted NBBO. Omit it and the half-spread
    figure is None rather than a guess; §5.4 wants all three reported, and a
    fabricated third is worse than an absent one.
    """
    legs = len(plan.legs) if plan and plan.legs else 0
    contracts = max(1, contracts)
    if legs == 0:
        return CostStress(pnl_mid_usd, pnl_mid_usd, None, source, 0, contracts)

    one_tick = round(pnl_mid_usd - _round_trip_ticks(legs, contracts), 2)

    half = None
    if half_spread_per_leg is not None and half_spread_per_leg >= 0:
        # Half the spread per leg, crossed on entry and on exit.
        half = round(
            pnl_mid_usd - (half_spread_per_leg * legs * 2 * 100.0 * contracts), 2
        )

    return CostStress(
        pnl_mid_usd=round(pnl_mid_usd, 2),
        pnl_1tick_usd=one_tick,
        pnl_half_spread_usd=half,
        source=source,
        legs=legs,
        contracts=contracts,
    )


def effective_half_spread(bars) -> float | None:
    """Median half-spread per leg across observed minutes, from executions.

    Median rather than mean: a single wide print in a thin minute would drag a
    mean badly, and the question being asked is what a typical crossing cost.
    Returns None when no minute had two-sided trade — a one-sided tape cannot
    price a spread, and assuming one would defeat the purpose.
    """
    spreads = [s for b in bars if (s := getattr(b, "effective_spread", None)) is not None]
    if not spreads:
        return None
    spreads.sort()
    n = len(spreads)
    mid = spreads[n // 2] if n % 2 else (spreads[n // 2 - 1] + spreads[n // 2]) / 2.0
    return round(mid / 2.0, 4)
