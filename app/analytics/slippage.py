"""Modeled fill versus the fill you actually got (item 2.5).

Every backtest number in this system rests on an assumption about fills: that a
structure transacts at the midpoint of its legs, with commission subtracted and
no synthetic slippage. That assumption has never been checked against evidence,
even though the evidence exists — real Robinhood fills are already parsed by
`scripts/rh_sync.py`, with a per-share price and a timestamp on every leg.

This measures the gap. It answers the question the cost-stress module can only
assume an answer to: is one tick per leg the right stress, or is the real number
two ticks, or half of one?

DIRECTION CONVENTION: slippage is reported as **cost**, positive when the real
fill was worse than modeled. A debit filled above the model and a credit filled
below it are both positive. Sign errors here would invert the conclusion, so the
convention is enforced in one place — `_signed_cost` — rather than at each call
site.

The output is diagnostic, never corrective. Nothing here rewrites a stored
outcome: a measured slippage informs the stress assumption for FUTURE analysis
and is reported alongside grades, not folded into them.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class FillComparison:
    """One structure: what we assumed, what happened."""

    decision_id: str
    symbol: str
    modeled_net_per_share: float
    actual_net_per_share: float
    contracts: int
    legs: int

    @property
    def slippage_per_share(self) -> float:
        """Positive = the real fill was worse than modeled."""
        return round(_signed_cost(self.modeled_net_per_share, self.actual_net_per_share), 4)

    @property
    def slippage_usd(self) -> float:
        return round(self.slippage_per_share * 100.0 * max(1, self.contracts), 2)

    @property
    def slippage_ticks_per_leg(self) -> float | None:
        """The comparable unit: how many one-cent ticks per leg this cost.

        This is the number that validates or refutes the one-tick stress
        assumption in `cost_stress.py`.
        """
        if self.legs <= 0:
            return None
        return round(self.slippage_per_share / (0.01 * self.legs), 2)


def _signed_cost(modeled: float, actual: float) -> float:
    """Cost of the real fill relative to the model, positive = worse.

    A DEBIT (net > 0) is worse when you pay more, so cost = actual - modeled.
    A CREDIT (net < 0) is worse when you receive less; the net is negative, so
    receiving less means the net rose toward zero — which is the same expression.
    One formula covers both, which is why it lives here and nowhere else.
    """
    return actual - modeled


@dataclass(frozen=True)
class SlippageReport:
    """The population answer, with its own sample size attached."""

    n: int
    median_per_share: float | None
    mean_per_share: float | None
    median_ticks_per_leg: float | None
    worst_per_share: float | None
    total_usd: float
    comparisons: list[FillComparison]

    @property
    def verdict(self) -> str:
        """Plain-language read on whether the one-tick stress is adequate.

        Deliberately conservative about small samples: below 10 paired fills the
        answer is that we do not know, not a number dressed as a finding.
        """
        if self.n < 10:
            return (
                f"UNDETERMINED — {self.n} paired fill(s). Need >=10 before the "
                "modeled-fill assumption can be called validated or refuted."
            )
        t = self.median_ticks_per_leg
        if t is None:
            return "UNDETERMINED — leg counts unavailable."
        if t <= 1.0:
            return (
                f"one-tick stress is ADEQUATE — real slippage runs {t:.2f} "
                f"ticks/leg (median, n={self.n})."
            )
        return (
            f"one-tick stress UNDERSTATES cost — real slippage runs {t:.2f} "
            f"ticks/leg (median, n={self.n}). H4 should be read against the "
            "half-spread column, not the one-tick headline."
        )


def compare_fills(comparisons: list[FillComparison]) -> SlippageReport:
    """Summarise modeled-vs-actual across paired structures."""
    if not comparisons:
        return SlippageReport(0, None, None, None, None, 0.0, [])

    per_share = [c.slippage_per_share for c in comparisons]
    ticks = [t for c in comparisons if (t := c.slippage_ticks_per_leg) is not None]
    return SlippageReport(
        n=len(comparisons),
        median_per_share=round(median(per_share), 4),
        mean_per_share=round(sum(per_share) / len(per_share), 4),
        median_ticks_per_leg=round(median(ticks), 2) if ticks else None,
        worst_per_share=round(max(per_share), 4),
        total_usd=round(sum(c.slippage_usd for c in comparisons), 2),
        comparisons=comparisons,
    )


def pair_snapshot_to_episode(snapshot, episode) -> FillComparison | None:
    """Pair one warehoused decision with the real fills that executed it.

    Returns None unless the leg sets match exactly. A near-match is not a match:
    comparing a modeled 500/505 spread against a real 500/510 fill would produce
    a slippage number that is really a strike difference.
    """
    plan = getattr(snapshot, "trade_plan", None)
    if plan is None or not plan.legs:
        return None

    modeled_key = tuple(sorted(
        (leg.option_type.value, round(leg.strike, 4), leg.expiration.isoformat())
        for leg in plan.legs
    ))
    try:
        actual_key = tuple(sorted(
            (t, round(s, 4), e) for (t, s, e) in
            [(k[0], k[1], k[2]) for k in [tuple(x) for x in episode.leg_key()]]
        ))
    except (TypeError, ValueError, IndexError):
        return None
    if modeled_key != actual_key:
        return None

    actual_net = getattr(episode, "net_entry_per_share", None)
    if callable(actual_net):
        actual_net = actual_net()
    if actual_net is None:
        return None

    return FillComparison(
        decision_id=snapshot.decision_id,
        symbol=snapshot.symbol,
        modeled_net_per_share=snapshot.entry_net_per_share,
        actual_net_per_share=float(actual_net),
        contracts=max(1, snapshot.contracts),
        legs=len(plan.legs),
    )
