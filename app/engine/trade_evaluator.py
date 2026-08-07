"""Grade a trade the human proposes, with the account deliberately out of scope.

The scanner asks "what should I look at, given $100 per trade and $300 of heat?"
This asks "here is a trade; what is wrong with it?" — no budget cap, no portfolio
heat, no position count. The two are different questions and this module shares
none of the account plumbing.

Where the account limits actually leak into selection, and how they are removed:

  * `app/engine/strategy_selector.py` turns `RiskPolicy` into a `max_debit_usd`
    ceiling. Not used here — the alternative is selected with an infinite cap.
  * `OptionLiquidityConfig.max_mid_price` defaults to 25.0 with the comment
    "keeps 1-lot affordable for small acct". That is a budget constraint wearing
    a liquidity costume, so the evaluator raises it out of the way. The genuine
    liquidity floors (OI, volume, spread) stay, because a structure nobody
    trades is a bad suggestion regardless of what it costs.

READ-ONLY BY CONSTRUCTION. This module never writes a decision snapshot. The
capture corpus has been polluted twice by code that persisted as a side effect
of being called, and `tests/test_trade_evaluator_isolation.py` exists to keep
that from happening a third time. Persistence lives in the API layer, in its own
table, behind an explicit flag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.config import settings
from app.domain.enums import Direction, OptionType
from app.domain.evaluation import (
    NA_NO_DATA,
    NA_NOT_IMPLEMENTED,
    Dimension,
    Fact,
    Improvement,
    PricedLeg,
    PricedStructure,
    StructureType,
    TradeEvaluation,
    Verdict,
)
from app.domain.market import EarningsEvent
from app.domain.options import IVContext, OptionChain, OptionContract
from app.engine.contract_selection import (
    SelectionConfig,
    select_long_contract,
    select_vertical_spread,
)
from app.engine.liquidity import OptionLiquidityConfig
from app.logging_config import get_logger
from app.quant.probability import (
    move_to_breakeven_pct,
    probability_of_profit,
    what_has_to_happen,
)

log = get_logger(__name__)

# How many expirations to pull. Wide enough that a 45d horizon still finds a
# chain, since the horizon is resolved against what the provider actually lists
# rather than against a calendar we assume.
CHAIN_EXPIRATIONS = 12

# Liquidity floors for BUILDING the alternative. Same OI/volume/spread bars the
# scanner uses; the affordability cap is lifted (see module docstring).
_EVAL_LIQ = OptionLiquidityConfig(max_mid_price=100_000.0)

_HORIZON_RE = re.compile(r"^\s*(\d+)\s*([dwm])\s*$", re.IGNORECASE)
_UNIT_DAYS = {"d": 1, "w": 7, "m": 30}

# Composite weights. Liquidity and probability carry the most because they are
# the two that need no calibration to be true: a spread you cannot get filled at
# and a break-even the market itself prices as unlikely are both facts about the
# trade, not forecasts about the world.
_WEIGHTS = {
    "cost_structure": 0.20,
    "probability": 0.25,
    "liquidity": 0.25,
    "iv_context": 0.10,
    "timing": 0.10,
    "alternative": 0.10,
}
_ORDER = list(_WEIGHTS)


@dataclass(frozen=True)
class EvaluationInputs:
    """Everything fetched from providers, so scoring is a pure function of it."""

    chain: OptionChain | None = None
    iv: IVContext | None = None
    earnings: EarningsEvent | None = None
    spot: float | None = None


# ---------------------------------------------------------------------------
# Horizon -> a real expiration
# ---------------------------------------------------------------------------


def parse_horizon(horizon: str) -> tuple[int | None, date | None]:
    """Return (target_days, explicit_date). Exactly one is non-None on success.

    Accepts `0d`/`3d`/`2w`/`6m` or an ISO date. Returns (None, None) when the
    input is unusable — the caller reports that as a gap rather than guessing a
    default horizon, because guessing would silently evaluate a different trade
    from the one asked about.
    """
    raw = (horizon or "").strip()
    if not raw:
        return None, None
    try:
        return None, date.fromisoformat(raw)
    except ValueError:
        pass
    m = _HORIZON_RE.match(raw)
    if not m:
        return None, None
    return int(m.group(1)) * _UNIT_DAYS[m.group(2).lower()], None


def resolve_expiration(
    chain: OptionChain, horizon: str, *, as_of: date
) -> tuple[date | None, str]:
    """Snap a horizon to a LISTED expiration and say which one it landed on.

    "3d" on a Thursday and "3d" on a Monday are different contracts. Returning
    the resolved date with a note is the difference between an evaluation the
    reader can reproduce and one they have to trust.
    """
    listed = sorted({c.expiration for c in chain.contracts if c.expiration >= as_of})
    if not listed:
        return None, "no expirations at or after today in the chain"

    target_days, explicit = parse_horizon(horizon)
    if explicit is not None:
        if explicit in listed:
            return explicit, f"exact listed expiry {explicit}"
        nearest = min(listed, key=lambda d: abs((d - explicit).days))
        return nearest, f"{explicit} is not listed; used nearest listed expiry {nearest}"
    if target_days is None:
        return None, (
            f"could not read a horizon from {horizon!r} — "
            "use 0d/3d/2w/45d or an ISO date"
        )

    want = as_of.fromordinal(as_of.toordinal() + target_days)
    nearest = min(listed, key=lambda d: abs((d - want).days))
    got = (nearest - as_of).days
    if got == target_days:
        return nearest, f"{horizon} resolved to {nearest} ({got} DTE)"
    return nearest, (
        f"{horizon} (~{target_days}d) resolved to the nearest listed expiry "
        f"{nearest} ({got} DTE)"
    )


# ---------------------------------------------------------------------------
# Pricing a structure off the chain
# ---------------------------------------------------------------------------


def _find(
    chain: OptionChain, expiration: date, strike: float, otype: OptionType
) -> OptionContract | None:
    for c in chain.contracts:
        if c.expiration == expiration and c.option_type == otype and abs(c.strike - strike) < 1e-6:
            return c
    return None


def _leg(c: OptionContract, action: str) -> PricedLeg:
    return PricedLeg(
        action=action, option_type=c.option_type.value, strike=c.strike,
        expiration=c.expiration, bid=c.bid, ask=c.ask, mid=c.mid,
        delta=c.greeks.delta, implied_volatility=c.implied_volatility,
        open_interest=c.open_interest, volume=c.volume,
    )


def _marketable(long_leg: OptionContract, short_leg: OptionContract | None) -> float | None:
    """Cost to open paying the spread: buy at the ask, sell at the bid.

    None when either side of the book is missing — a fill cost we cannot observe
    is not the same as a cheap one.
    """
    if long_leg.ask is None:
        return None
    if short_leg is None:
        return round(long_leg.ask, 4)
    if short_leg.bid is None:
        return None
    return round(long_leg.ask - short_leg.bid, 4)


def price_structure(
    *,
    chain: OptionChain,
    structure: StructureType,
    expiration: date,
    long_strike: float,
    short_strike: float | None,
    as_of: date,
) -> tuple[PricedStructure | None, str]:
    """Price a specific structure off the live chain. Returns (priced, error)."""
    otype = OptionType.CALL if structure.is_bullish else OptionType.PUT
    long_leg = _find(chain, expiration, long_strike, otype)
    if long_leg is None:
        return None, f"no listed {otype.value} at strike {long_strike} expiring {expiration}"
    if long_leg.mid is None or long_leg.mid <= 0:
        return None, f"{otype.value} {long_strike} has no usable price (bid/ask/mark all absent)"

    short_leg: OptionContract | None = None
    if structure.is_spread:
        if short_strike is None:
            return None, "a debit spread needs both strikes"
        short_leg = _find(chain, expiration, short_strike, otype)
        if short_leg is None:
            return None, f"no listed {otype.value} at strike {short_strike} expiring {expiration}"
        if short_leg.mid is None or short_leg.mid <= 0:
            return None, f"{otype.value} {short_strike} has no usable price"
        # A debit vertical is long the nearer-the-money leg.
        if structure.is_bullish and short_strike <= long_strike:
            return None, "for a call debit spread the short strike must be ABOVE the long strike"
        if not structure.is_bullish and short_strike >= long_strike:
            return None, "for a put debit spread the short strike must be BELOW the long strike"

    debit = long_leg.mid - (short_leg.mid if short_leg else 0.0)
    if debit <= 0:
        return None, "this structure prices to a credit, not a debit — not evaluable here"

    width = abs(short_leg.strike - long_leg.strike) if short_leg else None
    max_loss = round(debit * 100, 2)
    max_profit = round((width - debit) * 100, 2) if width is not None else None
    breakeven = (
        long_leg.strike + debit if structure.is_bullish else long_leg.strike - debit
    )
    rr = round(max_profit / max_loss, 3) if max_profit is not None and max_loss > 0 else None

    spot = chain.underlying_price
    iv = long_leg.implied_volatility
    dte = long_leg.dte(as_of)
    pop = None
    if spot and spot > 0 and iv and iv > 0 and dte >= 0:
        pop = probability_of_profit(
            spot=spot, breakeven=breakeven, iv=iv, days=float(dte),
            bullish=structure.is_bullish,
        )

    legs = [_leg(long_leg, "buy")]
    if short_leg is not None:
        legs.append(_leg(short_leg, "sell"))

    return PricedStructure(
        structure=structure, expiration=expiration, dte=dte, legs=legs,
        net_debit_per_share=round(debit, 4), max_loss_usd=max_loss,
        max_profit_usd=max_profit, width=width, breakeven=round(breakeven, 4),
        reward_to_risk=rr, probability_of_profit=pop,
        marketable_debit_per_share=_marketable(long_leg, short_leg),
    ), ""


def build_alternative(
    *, chain: OptionChain, structure: StructureType, expiration: date, as_of: date
) -> PricedStructure | None:
    """What the platform's own selector would pick at this expiry, unconstrained.

    Pinned to the resolved expiration (min_dte == max_dte == its DTE) so the
    comparison isolates STRIKE choice — a suggestion at a different expiry would
    be answering a question the user did not ask.
    """
    dte = (expiration - as_of).days
    if dte < 0:
        return None
    direction = Direction.BULLISH if structure.is_bullish else Direction.BEARISH
    sel = SelectionConfig(min_dte=dte, max_dte=dte)

    if structure.is_spread:
        choice = select_vertical_spread(
            chain, direction, as_of,
            max_debit_usd=float("inf"),  # budget-blind, by design
            sel=sel, liq=_EVAL_LIQ,
        )
        if choice is None:
            return None
        priced, _ = price_structure(
            chain=chain, structure=structure, expiration=expiration,
            long_strike=choice.long_leg.strike, short_strike=choice.short_leg.strike,
            as_of=as_of,
        )
        return priced

    pick = select_long_contract(chain, direction, as_of, sel=sel, liq=_EVAL_LIQ)
    if pick is None:
        return None
    priced, _ = price_structure(
        chain=chain, structure=structure, expiration=expiration,
        long_strike=pick.contract.strike, short_strike=None, as_of=as_of,
    )
    return priced


# ---------------------------------------------------------------------------
# The six dimensions
# ---------------------------------------------------------------------------


def _na(key: str, label: str, reason: str, why: str) -> Dimension:
    return Dimension(
        key=key, label=label, verdict=Verdict.NOT_ASSESSED, score=None,
        headline=why, unavailable=reason,
    )


def _band(value: float, strong: float, ok: float, weak: float, *, lower_is_better: bool) -> Verdict:
    """Map a measurement onto a verdict. Thresholds are passed in so every call
    site names its own bar in-line and the bar is visible next to the number."""
    if lower_is_better:
        if value <= strong:
            return Verdict.STRONG
        if value <= ok:
            return Verdict.ACCEPTABLE
        if value <= weak:
            return Verdict.WEAK
        return Verdict.FAIL
    if value >= strong:
        return Verdict.STRONG
    if value >= ok:
        return Verdict.ACCEPTABLE
    if value >= weak:
        return Verdict.WEAK
    return Verdict.FAIL


_VERDICT_SCORE = {
    Verdict.STRONG: 1.0,
    Verdict.ACCEPTABLE: 0.7,
    Verdict.WEAK: 0.4,
    Verdict.FAIL: 0.1,
}


def dim_cost_structure(s: PricedStructure, spot: float | None) -> Dimension:
    """What you pay against what the structure can possibly return.

    For a spread the measure is cost drag — the debit as a fraction of the width.
    Pay 65% of the width and you need the underlying to travel most of the way to
    the short strike just to break even, and the best case is a 0.54:1 return.
    For a single long leg there is no width, so the measure is how much of the
    premium is extrinsic: the part that decays to zero if price does nothing.
    """
    facts: list[Fact] = []
    facts.append(Fact(label="Net debit", value=f"${s.net_debit_per_share:.2f}/share",
                      note=f"${s.max_loss_usd:.0f} max loss per contract"))
    if s.breakeven is not None:
        facts.append(Fact(label="Break-even", value=f"${s.breakeven:.2f}"))

    if s.width is not None and s.width > 0:
        drag = s.net_debit_per_share / s.width
        facts.append(Fact(label="Width", value=f"${s.width:.2f}"))
        facts.append(Fact(label="Cost drag", value=f"{drag:.0%} of width",
                          note="strong <=35%, acceptable <=50%, weak <=65%"))
        facts.append(Fact(
            label="Max profit",
            value=f"${s.max_profit_usd:.0f}" if s.max_profit_usd is not None else NA_NO_DATA,
            note=f"{s.reward_to_risk}:1 reward-to-risk" if s.reward_to_risk else "",
        ))
        verdict = _band(drag, 0.35, 0.50, 0.65, lower_is_better=True)
        head = (
            f"You pay {drag:.0%} of the ${s.width:.2f} width; "
            f"best case returns {s.reward_to_risk}:1."
            if s.reward_to_risk
            else f"You pay {drag:.0%} of the ${s.width:.2f} width."
        )
        return Dimension(key="cost_structure", label="Cost & structure",
                         verdict=verdict, score=_VERDICT_SCORE[verdict],
                         headline=head, facts=facts)

    # Single long leg: upside is uncapped, so drag is meaningless. Extrinsic is
    # the real cost question — it is the part you lose to time if you are right
    # about direction but not about timing.
    if spot is None or spot <= 0:
        return _na("cost_structure", "Cost & structure", NA_NO_DATA,
                   "No spot price, so intrinsic/extrinsic cannot be split.")
    long_leg = s.legs[0]
    intrinsic = (
        max(0.0, spot - long_leg.strike) if s.structure.is_bullish
        else max(0.0, long_leg.strike - spot)
    )
    extrinsic = max(0.0, s.net_debit_per_share - intrinsic)
    ratio = extrinsic / s.net_debit_per_share if s.net_debit_per_share > 0 else 1.0
    facts.append(Fact(label="Intrinsic", value=f"${intrinsic:.2f}/share"))
    facts.append(Fact(label="Extrinsic (time value)", value=f"${extrinsic:.2f}/share",
                      note=f"{ratio:.0%} of the premium; strong <=40%, weak <=85%"))
    facts.append(Fact(label="Max profit", value="uncapped",
                      note="single long leg — no short strike caps the upside"))
    verdict = _band(ratio, 0.40, 0.65, 0.85, lower_is_better=True)
    return Dimension(
        key="cost_structure", label="Cost & structure", verdict=verdict,
        score=_VERDICT_SCORE[verdict],
        headline=(f"{ratio:.0%} of the ${s.net_debit_per_share:.2f} premium is time value "
                  f"and decays to zero by expiry."),
        facts=facts,
    )


def dim_probability(s: PricedStructure, symbol: str, spot: float | None) -> Dimension:
    """The market's own implied odds, at the IV it is quoting right now.

    Black-Scholes at the long leg's IV — modelled, not calibrated, and labelled
    as such. Its value is not that it is right; it is that it is INDEPENDENT of
    the thesis. A structure the market prices as a 1-in-6 shot is that whether or
    not the direction call is good.
    """
    if s.probability_of_profit is None:
        return _na("probability", "Probability", NA_NO_DATA,
                   "IV or spot missing, so the odds could not be modelled. "
                   "An unmodellable probability is not evidence of a good one.")
    pop = s.probability_of_profit
    facts = [Fact(label="P(profit) at expiry", value=f"{pop:.0%}",
                  note=f"Black-Scholes at {s.legs[0].implied_volatility:.1%} IV"
                  if s.legs[0].implied_volatility else "Black-Scholes")]
    if spot and s.breakeven:
        move = move_to_breakeven_pct(spot, s.breakeven)
        if move is not None:
            facts.append(Fact(label="Move to break-even", value=f"{move:+.1f}%",
                              note=f"${spot:.2f} -> ${s.breakeven:.2f}"))
        facts.append(Fact(
            label="What has to happen",
            value=what_has_to_happen(symbol=symbol, spot=spot, breakeven=s.breakeven,
                                     days=s.dte, bullish=s.structure.is_bullish) or NA_NO_DATA,
        ))
    verdict = _band(pop, settings.display_pop_ok, settings.display_pop_bad, 0.25,
                    lower_is_better=False)
    return Dimension(
        key="probability", label="Probability (modelled)", verdict=verdict,
        score=_VERDICT_SCORE[verdict],
        headline=f"The market's implied odds of this finishing profitable are {pop:.0%}.",
        facts=facts,
    )


def dim_liquidity(s: PricedStructure) -> Dimension:
    """What it costs to get in and out — the dimension that needs no calibration.

    Cost drag here is the ROUND-TRIP spread tax against defined max loss: pay the
    ask on the way in and hit the bid on the way out, twice the half-spread, as a
    fraction of what you are risking. On a cheap short-dated spread this is
    routinely a double-digit percentage of max loss before direction is
    considered, which is why it is weighted equally with probability.
    """
    spreads = [
        (leg, (leg.ask - leg.bid) / leg.mid)
        for leg in s.legs
        if leg.ask is not None and leg.bid is not None and leg.mid and leg.mid > 0
    ]
    if not spreads:
        return _na("liquidity", "Liquidity & execution", NA_NO_DATA,
                   "No two-sided quotes on the legs, so fill cost is unknown.")

    worst_leg, worst = max(spreads, key=lambda t: t[1])
    facts = [Fact(label="Widest leg spread", value=f"{worst:.1%} of mid",
                  note=f"{worst_leg.option_type} {worst_leg.strike:g}")]

    ois = [leg.open_interest for leg in s.legs if leg.open_interest is not None]
    vols = [leg.volume for leg in s.legs if leg.volume is not None]
    facts.append(Fact(label="Min open interest",
                      value=f"{min(ois):,}" if ois else NA_NO_DATA,
                      note="thin OI widens the exit, not just the entry"))
    facts.append(Fact(label="Min volume today",
                      value=f"{min(vols):,}" if vols else NA_NO_DATA))

    drag = None
    if s.marketable_debit_per_share is not None and s.max_loss_usd > 0:
        entry_slip = s.marketable_debit_per_share - s.net_debit_per_share
        round_trip = 2 * entry_slip * 100  # in and out, per contract
        drag = round_trip / s.max_loss_usd
        facts.append(Fact(
            label="Marketable entry",
            value=f"${s.marketable_debit_per_share:.2f}/share",
            note=f"vs ${s.net_debit_per_share:.2f} at mid — "
                 f"${entry_slip * 100:.0f} worse per contract",
        ))
        facts.append(Fact(
            label="Round-trip spread tax", value=f"{drag:.0%} of max loss",
            note=f"good <={settings.display_cost_drag_good:.0%}, "
                 f"bad >{settings.display_cost_drag_bad:.0%}",
        ))

    # The binding constraint is whichever is worse: the quoted spread or the
    # round-trip tax it implies against what you are risking.
    v_spread = _band(worst, 0.05, 0.10, 0.20, lower_is_better=True)
    if drag is None:
        verdict, head = v_spread, f"Widest leg is quoted {worst:.1%} wide."
    else:
        v_drag = _band(drag, settings.display_cost_drag_good,
                       settings.display_cost_drag_bad, 0.50, lower_is_better=True)
        verdict = min(v_spread, v_drag, key=lambda v: _VERDICT_SCORE[v])
        head = (f"Getting in and out costs about {drag:.0%} of max loss "
                f"before the underlying moves at all.")
    return Dimension(key="liquidity", label="Liquidity & execution", verdict=verdict,
                     score=_VERDICT_SCORE[verdict], headline=head, facts=facts)


def dim_iv_context(iv: IVContext | None) -> Dimension:
    """Are you buying premium cheap or expensive?

    Polarity matters and is easy to get backwards: these are all DEBIT
    structures, so you are long vol. High IV rank is against you — you pay up
    front and an IV drop hurts even when direction is right.
    """
    if iv is None or iv.iv_rank is None:
        return _na("iv_context", "IV context", NA_NO_DATA,
                   "No IV rank for this symbol, so cheap-vs-expensive premium "
                   "cannot be judged.")
    facts = [Fact(label="IV rank", value=f"{iv.iv_rank:.0%}",
                  note=f"source: {iv.iv_rank_source or NA_NO_DATA} — "
                       "debit structures are LONG vol, so lower is better")]
    if iv.iv30 is not None:
        facts.append(Fact(label="IV30", value=f"{iv.iv30:.1%}"))
    if iv.hv20 is not None:
        facts.append(Fact(label="HV20 (realized)", value=f"{iv.hv20:.1%}"))
    ratio = iv.iv_hv_ratio
    if ratio is not None:
        facts.append(Fact(label="IV / HV", value=f"{ratio:.2f}",
                          note=">1 means options are pricing more movement than "
                               "the stock has actually delivered"))
    verdict = _band(iv.iv_rank, 0.30, 0.55, 0.75, lower_is_better=True)
    head = (
        f"IV rank {iv.iv_rank:.0%} — you are buying "
        f"{'expensive' if iv.iv_rank > 0.55 else 'reasonably priced' if iv.iv_rank > 0.30 else 'cheap'}"
        " premium."
    )
    return Dimension(key="iv_context", label="IV context", verdict=verdict,
                     score=_VERDICT_SCORE[verdict], headline=head, facts=facts)


def dim_timing(s: PricedStructure, earnings: EarningsEvent | None, as_of: date) -> Dimension:
    """Does anything scheduled land inside the holding window?

    Earnings inside the window is not automatically disqualifying — it is a
    different trade, priced with a vol crush on the other side of it. The
    evaluator's job is to make sure it was a choice rather than a surprise.
    """
    facts = [Fact(label="Days to expiry", value=f"{s.dte}",
                  note=f"expires {s.expiration}")]
    verdict = Verdict.STRONG
    head = f"{s.dte} days to expiry with no scheduled earnings inside the window."

    if earnings is None:
        facts.append(Fact(label="Earnings", value=NA_NO_DATA,
                          note="no earnings date from the calendar feed — "
                               "absence of a date is not evidence of no event"))
        verdict = Verdict.ACCEPTABLE
        head = f"{s.dte} days to expiry; earnings date unknown for this symbol."
    else:
        inside = as_of <= earnings.report_date <= s.expiration
        facts.append(Fact(
            label="Earnings", value=str(earnings.report_date),
            note=(f"{earnings.time_of_day or 'time unknown'} — "
                  f"{'INSIDE' if inside else 'outside'} the holding window"),
        ))
        if inside:
            verdict = Verdict.WEAK
            head = (f"Earnings on {earnings.report_date} falls inside the window. "
                    "A long-vol debit structure through an event pays the pre-event "
                    "premium and eats the post-event IV crush.")

    if s.dte == 0:
        facts.append(Fact(label="0DTE", value="yes",
                          note="OBSERVATION ONLY — grades for this bucket are "
                               "quarantined from calibration (Amendment 3)"))
        verdict = min(verdict, Verdict.WEAK, key=lambda v: _VERDICT_SCORE[v])
        head += " Same-day expiry leaves no time for the thesis to be wrong first."

    return Dimension(key="timing", label="Timing & events", verdict=verdict,
                     score=_VERDICT_SCORE[verdict], headline=head, facts=facts)


def dim_alternative(
    proposed: PricedStructure | None, alt: PricedStructure | None
) -> Dimension:
    """How does the proposed structure compare to the best one at this expiry?

    Only meaningful when the user supplied strikes. When they did not, the
    graded structure IS the selector's pick and there is nothing to contrast —
    reported as NOT_IMPLEMENTED rather than silently scored, because "no gap"
    and "no comparison" are different statements.
    """
    if proposed is None:
        return _na("alternative", "Versus the best available", NA_NOT_IMPLEMENTED,
                   "No strikes were supplied, so this IS the selector's own pick "
                   "— there is nothing to compare it against.")
    if alt is None:
        return _na("alternative", "Versus the best available", NA_NO_DATA,
                   "The selector found no liquid structure at this expiry to "
                   "compare against.")

    same = (
        len(proposed.legs) == len(alt.legs)
        # strict= is safe: the length check above short-circuits first.
        and all(abs(a.strike - b.strike) < 1e-6
                for a, b in zip(proposed.legs, alt.legs, strict=True))
    )
    facts = [
        Fact(label="Your structure",
             value=" / ".join(f"{leg.strike:g}" for leg in proposed.legs),
             note=(f"${proposed.net_debit_per_share:.2f} debit, "
                   f"POP {proposed.probability_of_profit:.0%}"
                   if proposed.probability_of_profit is not None
                   else f"${proposed.net_debit_per_share:.2f} debit, POP {NA_NO_DATA}")),
        Fact(label="Selector's pick",
             value=" / ".join(f"{leg.strike:g}" for leg in alt.legs),
             note=(f"${alt.net_debit_per_share:.2f} debit, "
                   f"POP {alt.probability_of_profit:.0%}"
                   if alt.probability_of_profit is not None
                   else f"${alt.net_debit_per_share:.2f} debit, POP {NA_NO_DATA}")),
    ]
    if same:
        return Dimension(
            key="alternative", label="Versus the best available", verdict=Verdict.STRONG,
            score=1.0, facts=facts,
            headline="Your strikes ARE what the selector would pick at this expiry.",
        )

    pp, ap = proposed.probability_of_profit, alt.probability_of_profit
    if pp is None or ap is None:
        return _na("alternative", "Versus the best available", NA_NO_DATA,
                   "Odds could not be modelled for both structures, so the gap "
                   "cannot be quantified.")
    gap = ap - pp
    facts.append(Fact(label="Probability gap", value=f"{gap:+.0%}",
                      note="how much more likely the selector's pick is to profit"))
    verdict = _band(gap, 0.02, 0.08, 0.15, lower_is_better=True)
    head = (
        f"The selector would pick different strikes for {ap:.0%} odds against your "
        f"{pp:.0%} — a {gap:+.0%} gap."
        if gap > 0
        else f"Your strikes model BETTER odds ({pp:.0%}) than the selector's pick ({ap:.0%})."
    )
    return Dimension(key="alternative", label="Versus the best available", verdict=verdict,
                     score=_VERDICT_SCORE[verdict], headline=head, facts=facts)


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------


def grade_from(dimensions: list[Dimension]) -> tuple[str, float | None, int]:
    """Weighted mean over ASSESSED dimensions only, renormalized.

    A dimension with no data contributes nothing rather than a zero, so a missing
    feed cannot masquerade as a measured failure. Any single FAIL caps the grade
    at D: a structure that fails one hard bar is not rescued by scoring well on
    the others, and averaging would let it be.
    """
    scored = [d for d in dimensions if d.score is not None and d.key in _WEIGHTS]
    if not scored:
        return "", None, 0
    total_w = sum(_WEIGHTS[d.key] for d in scored)
    composite = sum(_WEIGHTS[d.key] * d.score for d in scored) / total_w  # type: ignore[operator]
    composite = round(composite, 4)

    if composite >= 0.80:
        grade = "A"
    elif composite >= 0.65:
        grade = "B"
    elif composite >= 0.50:
        grade = "C"
    elif composite >= 0.35:
        grade = "D"
    else:
        grade = "F"

    if any(d.verdict == Verdict.FAIL for d in scored) and grade in ("A", "B", "C"):
        grade = "D"
    return grade, composite, len(scored)


def improvements_for(
    dimensions: list[Dimension],
    proposed: PricedStructure | None,
    alt: PricedStructure | None,
) -> list[Improvement]:
    """Concrete changes, derived from the dimensions that scored badly.

    Every entry names the measured quantity that moves. "Consider a different
    strike" is advice; "the 770/775 prices 38% odds against your 12%" is a
    change the reader can check against the same numbers the grade came from.
    """
    out: list[Improvement] = []
    by_key = {d.key: d for d in dimensions}

    def bad(key: str) -> bool:
        d = by_key.get(key)
        return d is not None and d.verdict in (Verdict.WEAK, Verdict.FAIL)

    if proposed is not None and alt is not None and bad("alternative"):
        ap = alt.probability_of_profit
        pp = proposed.probability_of_profit
        strikes = "/".join(f"{leg.strike:g}" for leg in alt.legs)
        out.append(Improvement(
            dimension="alternative",
            suggestion=f"Use the {strikes} structure at the same {alt.expiration} expiry.",
            impact=(f"POP {pp:.0%} -> {ap:.0%} for ${alt.net_debit_per_share:.2f} vs "
                    f"${proposed.net_debit_per_share:.2f} debit"
                    if pp is not None and ap is not None else ""),
        ))

    if bad("cost_structure") and proposed is not None and proposed.width is not None:
        out.append(Improvement(
            dimension="cost_structure",
            suggestion="Widen the spread or move the long leg closer to the money.",
            impact=(f"you are paying {proposed.net_debit_per_share / proposed.width:.0%} "
                    f"of the ${proposed.width:.2f} width; under 50% leaves the payoff "
                    "worth the risk"),
        ))

    if bad("probability"):
        out.append(Improvement(
            dimension="probability",
            suggestion="Move the long strike closer to the money, or go further out in time.",
            impact="both shorten the distance to break-even the underlying has to cover",
        ))

    if bad("liquidity"):
        out.append(Improvement(
            dimension="liquidity",
            suggestion="Trade a more liquid strike or expiry, and use a limit at the mid.",
            impact="the round-trip spread tax comes out of max loss before direction matters",
        ))

    if bad("iv_context"):
        out.append(Improvement(
            dimension="iv_context",
            suggestion="A debit structure is long vol; at this IV rank a spread "
                       "(which is short the wing) bleeds less than a single long leg.",
            impact="reduces vega exposure to an IV drop that would hurt even if you are right",
        ))

    if bad("timing"):
        out.append(Improvement(
            dimension="timing",
            suggestion="Move the expiry to the other side of the event, or accept "
                       "that this is an event trade and size it as one.",
            impact="avoids paying pre-event premium and eating the post-event IV crush",
        ))
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def evaluate(
    *,
    symbol: str,
    structure: StructureType,
    horizon: str,
    inputs: EvaluationInputs,
    long_strike: float | None = None,
    short_strike: float | None = None,
    now: datetime | None = None,
) -> TradeEvaluation:
    """Pure scoring over already-fetched inputs. No I/O, no persistence.

    Kept separate from the provider fan-out so the whole rubric is testable
    against fixtures without a network, which is the only way the thresholds
    stay checkable.
    """
    now = now or datetime.now(UTC)
    as_of = now.date()
    ev = TradeEvaluation(
        symbol=symbol, structure=structure, as_of=now, requested_horizon=horizon,
        spot=inputs.spot, evaluator_version=settings.trade_eval_version,
    )

    if inputs.chain is None or not inputs.chain.contracts:
        ev.errors["chain"] = "no option chain available for this symbol"
        ev.dimensions = [
            _na(k, k.replace("_", " ").title(), NA_NO_DATA, "No option chain.")
            for k in _ORDER
        ]
        ev.dimensions_total = len(_ORDER)
        return ev

    expiration, note = resolve_expiration(inputs.chain, horizon, as_of=as_of)
    ev.resolved_expiration, ev.horizon_note = expiration, note
    if expiration is None:
        ev.errors["horizon"] = note
        ev.dimensions = [
            _na(k, k.replace("_", " ").title(), NA_NO_DATA, note) for k in _ORDER
        ]
        ev.dimensions_total = len(_ORDER)
        return ev

    if long_strike is not None:
        proposed, err = price_structure(
            chain=inputs.chain, structure=structure, expiration=expiration,
            long_strike=long_strike, short_strike=short_strike, as_of=as_of,
        )
        if err:
            ev.errors["proposed"] = err
        ev.proposed = proposed

    ev.alternative = build_alternative(
        chain=inputs.chain, structure=structure, expiration=expiration, as_of=as_of
    )
    if ev.alternative is None:
        ev.errors["alternative"] = (
            "the selector found no structure clearing its liquidity and "
            "probability floors at this expiry"
        )

    # Grade what the user asked about when they named strikes; otherwise grade
    # the structure the tool built. `graded` says which, so the dimensions are
    # never ambiguous about their subject.
    target = ev.proposed or ev.alternative
    ev.graded = "proposed" if ev.proposed is not None else "alternative"
    if target is None:
        ev.dimensions = [
            _na(k, k.replace("_", " ").title(), NA_NO_DATA,
                "No priceable structure for these inputs.") for k in _ORDER
        ]
        ev.dimensions_total = len(_ORDER)
        return ev

    ev.dimensions = [
        dim_cost_structure(target, inputs.spot),
        dim_probability(target, symbol, inputs.spot),
        dim_liquidity(target),
        dim_iv_context(inputs.iv),
        dim_timing(target, inputs.earnings, as_of),
        dim_alternative(ev.proposed, ev.alternative),
    ]
    ev.grade, ev.composite, ev.dimensions_assessed = grade_from(ev.dimensions)
    ev.dimensions_total = len(ev.dimensions)
    ev.improvements = improvements_for(ev.dimensions, ev.proposed, ev.alternative)
    return ev
