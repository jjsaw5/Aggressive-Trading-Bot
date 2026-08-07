"""Proposed option structures — the concrete expression of a candidate.

One model covers both allowed shapes:

* a single long leg (long call / long put)
* a two-leg debit vertical (bull call spread / bear put spread)

Everything a report needs to state about the trade — debit, max loss, max
profit, breakeven, width — is computed here from leg quotes, once, so the
report, the scorer and the persistence layer cannot disagree about what the
trade costs.

`greeks_source` and `iv_source` travel with the numbers because the platform's
providers do not all supply Greeks; where Black-Scholes filled them in, the
surface says MODELED (CLAUDE.md §4, "Modeled is labeled").
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, computed_field

from app.domain.enums import OptionAction, OptionType, StrategyType
from app.domain.options import Greeks
from app.multiagent.models.measurements import Provenance


class ProposedLeg(BaseModel):
    """One option leg with its live market state at selection time."""

    option_symbol: str | None = None
    underlying: str
    expiration: date
    strike: float
    option_type: OptionType
    action: OptionAction

    bid: float | None = None
    ask: float | None = None
    mark: float | None = None
    last: float | None = None

    volume: int | None = None
    open_interest: int | None = None
    implied_volatility: float | None = None
    greeks: Greeks = Field(default_factory=Greeks)
    greeks_source: Provenance = Provenance.PROVIDER

    as_of: datetime
    delayed_minutes: int = 0
    source: str = "unknown"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spread_abs(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return round(self.ask - self.bid, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spread_pct(self) -> float | None:
        """Spread as a fraction of the mark. None when either side is missing.

        Deliberately not `(ask-bid)/ask` — dividing by the ask flatters a wide
        market. Mid is the honest denominator for what a round trip costs.
        """
        if self.bid is None or self.ask is None:
            return None
        mid = (self.bid + self.ask) / 2.0
        if mid <= 0:
            return None
        return round((self.ask - self.bid) / mid, 4)

    def dte(self, as_of: date) -> int:
        return (self.expiration - as_of).days


class ProposedStructure(BaseModel):
    """A complete, priced, defined-risk trade proposal.

    All money figures are **per contract** (i.e. per-share price x 100) unless
    the name says otherwise. `total_*` fields account for `contracts`.
    """

    structure_id: str
    candidate_id: str
    run_id: str
    ticker: str
    strategy_type: StrategyType
    legs: list[ProposedLeg] = Field(default_factory=list)

    underlying_price: float | None = None
    underlying_as_of: datetime | None = None

    # Entry pricing. `net_debit_per_share` is what you pay at the marks used;
    # `net_debit_at_ask` is the same trade crossing the full spread, which is
    # what the cost-drag figure is honest about.
    net_debit_per_share: float | None = None
    net_debit_at_ask_per_share: float | None = None

    contracts: int = 1

    max_loss_per_contract: float | None = None
    max_profit_per_contract: float | None = None
    breakeven: float | None = None
    width: float | None = None

    # Net position greeks, summed across legs with sign by action.
    net_delta: float | None = None
    net_gamma: float | None = None
    net_theta: float | None = None
    net_vega: float | None = None
    greeks_source: Provenance = Provenance.PROVIDER

    # Probability of profit from the market-implied distribution, or None when
    # uncomputable. Never defaulted.
    probability_of_profit: float | None = None
    pop_source: Provenance = Provenance.MODELED

    # Round-trip spread tax as a share of defined max loss.
    cost_drag_pct: float | None = None

    selection_notes: list[str] = Field(default_factory=list)
    selected_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_cost(self) -> float | None:
        if self.net_debit_per_share is None:
            return None
        return round(self.net_debit_per_share * 100.0 * self.contracts, 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_max_loss(self) -> float | None:
        if self.max_loss_per_contract is None:
            return None
        return round(self.max_loss_per_contract * self.contracts, 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_max_profit(self) -> float | None:
        if self.max_profit_per_contract is None:
            return None
        return round(self.max_profit_per_contract * self.contracts, 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reward_to_risk(self) -> float | None:
        """Defined max profit over defined max loss. None if either is unknown.

        For a long single option max profit is unbounded, so this is None by
        construction and the risk/reward rule uses the target-based figure
        instead. Reporting an invented cap here would be a fabricated number.
        """
        if self.max_profit_per_contract is None or not self.max_loss_per_contract:
            return None
        return round(self.max_profit_per_contract / self.max_loss_per_contract, 3)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def worst_leg_spread_pct(self) -> float | None:
        vals = [leg.spread_pct for leg in self.legs if leg.spread_pct is not None]
        if len(vals) != len(self.legs) or not vals:
            return None  # a leg with no two-sided market makes the whole thing unknown
        return round(max(vals), 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def min_open_interest(self) -> int | None:
        vals = [leg.open_interest for leg in self.legs if leg.open_interest is not None]
        if len(vals) != len(self.legs) or not vals:
            return None
        return min(vals)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def min_volume(self) -> int | None:
        vals = [leg.volume for leg in self.legs if leg.volume is not None]
        if len(vals) != len(self.legs) or not vals:
            return None
        return min(vals)

    @property
    def expiration(self) -> date | None:
        return self.legs[0].expiration if self.legs else None

    def dte(self, as_of: date) -> int | None:
        exp = self.expiration
        return None if exp is None else (exp - as_of).days

    def describe(self) -> str:
        if not self.legs:
            return f"{self.ticker} {self.strategy_type.value} (no legs)"
        exp = self.legs[0].expiration.isoformat()
        strikes = "/".join(f"{leg.strike:g}" for leg in self.legs)
        return f"{self.ticker} {exp} {strikes} {self.strategy_type.value}"
