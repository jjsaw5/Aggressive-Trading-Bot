"""Market-implied probability of profit for a defined-risk directional structure.

Turns the market's own implied volatility into an honest read on a trade's odds:
under a risk-neutral, zero-drift lognormal model of the underlying at expiry (the
Black-Scholes world), what is the chance the structure finishes past its
break-even? Deterministic and explainable — a closed-form normal CDF, no
simulation. Every debit structure we build (a long single leg or a defined-risk
debit vertical) is one-directional with a single break-even, so profit is exactly
"underlying on the right side of the break-even at expiry."

This is INFORMATIONAL — a sanity check on the payoff odds so a human can see a
low-probability, capped-payoff bet for what it is. It never gates or scores a
trade. It answers "what does the market imply my odds are?", not "should I do
this?".
"""

from __future__ import annotations

import math

_SQRT2 = math.sqrt(2.0)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def prob_finish_above(spot: float, target: float, iv: float, days: float) -> float | None:
    """Risk-neutral P(S_T >= target) under a zero-drift lognormal at `days` out.

    `iv` is the annualized implied volatility (e.g. 0.45 for 45%). Returns None when
    any input is degenerate (non-positive), so callers degrade rather than lie."""
    if spot <= 0 or target <= 0 or iv <= 0 or days <= 0:
        return None
    t = days / 365.0
    sigma = iv * math.sqrt(t)
    if sigma <= 0:
        return None
    # Zero-drift lognormal: ln(S_T) ~ Normal(ln(spot) - 0.5*sigma^2, sigma^2).
    # d2 is the standardized distance of the log-strike; P(S_T>=K) = N(d2).
    d2 = (math.log(spot / target) - 0.5 * sigma * sigma) / sigma
    return _norm_cdf(d2)


def probability_of_profit(
    *, spot: float, breakeven: float, iv: float, days: float, bullish: bool
) -> float | None:
    """Market-implied P(profit at expiry) for a single-break-even directional debit
    structure. Bullish structures profit above the break-even; bearish below it.
    Returns a probability in [0, 1], or None when inputs are unusable."""
    p_above = prob_finish_above(spot, breakeven, iv, days)
    if p_above is None:
        return None
    return round(p_above if bullish else 1.0 - p_above, 4)


def move_to_breakeven_pct(spot: float, breakeven: float) -> float | None:
    """Signed % the underlying must travel to reach break-even (>0 up, <0 down)."""
    if spot <= 0:
        return None
    return round((breakeven - spot) / spot * 100.0, 2)


def what_has_to_happen(
    *, symbol: str, spot: float, breakeven: float, days: int | None, bullish: bool
) -> str:
    """A plain-English one-liner: the concrete move the underlying must make, by
    when, just to break even — so the ask is explicit instead of implied. Empty
    string when it can't be computed (missing spot/break-even)."""
    move = move_to_breakeven_pct(spot, breakeven)
    if move is None or breakeven <= 0:
        return ""
    direction = "rise" if move > 0 else "fall"
    by = f" by expiry ({days}d)" if days is not None else " by expiry"
    already = ""
    # If price is already past break-even on the profitable side, say so plainly.
    if (bullish and spot >= breakeven) or (not bullish and spot <= breakeven):
        return (
            f"{symbol} is already past break-even (${breakeven:.2f}); it needs to stay "
            f"{'above' if bullish else 'below'} it{by} to keep the gain."
        )
    return (
        f"{symbol} must {direction} {abs(move):.1f}% to ${breakeven:.2f}{by} "
        f"just to break even{already}."
    )
