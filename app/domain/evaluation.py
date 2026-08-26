"""Trade evaluation: grade a structure the HUMAN proposes.

The scanner answers "what should I look at?" under this account's risk limits.
This answers a different question — "here is a trade I am considering; what is
wrong with it?" — with the account deliberately out of scope. No budget cap, no
portfolio heat, no position count. Only the trade.

WHAT THE GRADE IS, AND IS NOT
-----------------------------
It grades **construction**, not outcome. The conviction gate is RED and no
feature has cleared out-of-sample validation, so nothing here is entitled to
predict whether a trade makes money. What it CAN do without any calibration is
measure the things that are arithmetic (cost, breakeven, payoff), model-implied
(probability at current IV), or directly observable (spread, open interest,
earnings inside the window) — and say which of those the trade handles badly.

A trade can grade well and lose. A trade that grades badly is one where the
arithmetic, the odds at the market's own implied vol, or the execution cost are
against you before direction is even considered. That is the whole claim.

Dimensions score independently and a dimension with no data reports
`NOT_ASSESSED` with a sentinel reason. The composite is taken over the assessed
dimensions only, and always reports how many were assessed — a B over four of
six dimensions is not the same claim as a B over six, and the reader is told
which one they are looking at.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field

# Sentinels, never blanks (CLAUDE.md §4). The distinction is load-bearing:
# NO_DATA means the concept applies to this trade and the feed did not supply it;
# NOT_IMPLEMENTED means the system has no such concept for this structure.
NA_NO_DATA = "NA_no_data"
NA_NOT_IMPLEMENTED = "NA_not_implemented"


class StructureType(str, Enum):
    """The structures the evaluator can price.

    Deliberately limited to long, defined-risk debit structures — the ones this
    platform actually models end to end (breakeven, POP, max loss). A credit or
    undefined-risk structure would need a different risk model, and returning a
    confident grade for one we cannot price would be worse than refusing.
    """

    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    CALL_DEBIT_SPREAD = "call_debit_spread"
    PUT_DEBIT_SPREAD = "put_debit_spread"

    @property
    def is_spread(self) -> bool:
        return self in (StructureType.CALL_DEBIT_SPREAD, StructureType.PUT_DEBIT_SPREAD)

    @property
    def is_bullish(self) -> bool:
        return self in (StructureType.LONG_CALL, StructureType.CALL_DEBIT_SPREAD)


class Verdict(str, Enum):
    STRONG = "strong"
    ACCEPTABLE = "acceptable"
    WEAK = "weak"
    FAIL = "fail"
    NOT_ASSESSED = "not_assessed"


class Fact(BaseModel):
    """One measured number behind a dimension's verdict.

    `value` is a pre-formatted string so the sentinel discipline survives to the
    UI: a missing measurement renders as `NA_no_data`, never as a blank cell or
    a zero. `note` carries the threshold the value was judged against, so the
    verdict can be checked rather than trusted.
    """

    label: str
    value: str
    note: str = ""


class Dimension(BaseModel):
    key: str
    label: str
    verdict: Verdict
    # None exactly when verdict is NOT_ASSESSED. Absent stays absent — a
    # dimension we could not measure must not contribute a 0.0 that would drag
    # the composite down as if it had been measured and failed.
    score: float | None = None
    headline: str = ""
    facts: list[Fact] = Field(default_factory=list)
    # Set only when NOT_ASSESSED: NA_no_data or NA_not_implemented.
    unavailable: str | None = None


class PricedLeg(BaseModel):
    action: str  # "buy" | "sell"
    option_type: str
    strike: float
    expiration: date
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    delta: float | None = None
    implied_volatility: float | None = None
    open_interest: int | None = None
    volume: int | None = None


class PricedStructure(BaseModel):
    """A concrete, chain-priced structure — either the user's or the selector's."""

    structure: StructureType
    expiration: date
    dte: int
    legs: list[PricedLeg] = Field(default_factory=list)
    net_debit_per_share: float
    max_loss_usd: float  # per contract
    max_profit_usd: float | None = None  # None when uncapped (single long leg)
    width: float | None = None  # None for single-leg
    breakeven: float | None = None
    reward_to_risk: float | None = None
    probability_of_profit: float | None = None
    # Cost to open paying the spread (buy at ask, sell at bid) vs. the mid-based
    # debit above. Retail fills land nearer this than the mid.
    marketable_debit_per_share: float | None = None
    # Black-Scholes; no provider supplies greeks for every leg (CLAUDE.md §4).
    greeks_source: str = "black_scholes"


class Improvement(BaseModel):
    """A concrete change, not advice.

    `impact` names the measured quantity that moves and by how much, so the
    suggestion can be checked against the same numbers the grade came from.
    """

    dimension: str
    suggestion: str
    impact: str = ""


class TradeEvaluation(BaseModel):
    symbol: str
    structure: StructureType
    as_of: datetime
    # What the caller asked for vs. what the chain actually had. "3d" on a
    # Thursday and "3d" on a Monday are different contracts, so the expiry the
    # evaluation actually used is reported rather than implied.
    requested_horizon: str
    resolved_expiration: date | None = None
    horizon_note: str = ""

    spot: float | None = None
    proposed: PricedStructure | None = None  # the user's strikes, when supplied
    alternative: PricedStructure | None = None  # what the selector would pick
    graded: str = "proposed"  # which of the two the dimensions describe

    dimensions: list[Dimension] = Field(default_factory=list)
    grade: str = ""  # A/B/C/D/F, or "" when nothing could be assessed
    composite: float | None = None
    dimensions_assessed: int = 0
    dimensions_total: int = 0
    # Restates the limits of the grade wherever it is displayed. The scanner
    # learned this the hard way: a number shown without its caveat is read as a
    # number that has one.
    grade_claim: str = (
        "UNCALIBRATED. This grades the trade's CONSTRUCTION — cost, modelled odds "
        "at current IV, execution cost, IV context and timing conflicts. It is not "
        "a prediction of profit and no feature behind it has cleared out-of-sample "
        "validation."
    )
    improvements: list[Improvement] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)
    evaluator_version: str = ""
