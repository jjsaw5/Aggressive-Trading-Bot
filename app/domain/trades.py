"""Trade-plan, proposal, and paper-trade models."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from app.domain.enums import (
    Direction,
    ExitReason,
    OptionAction,
    OptionType,
    PaperTradeStatus,
    ProposalStatus,
    StrategyType,
)


class ContractLeg(BaseModel):
    """One leg of a (possibly multi-leg) options structure."""

    symbol: str  # underlying
    option_symbol: str | None = None
    action: OptionAction
    option_type: OptionType
    strike: float
    expiration: date
    quantity: int = Field(ge=1)
    entry_price: float  # per-share option price (mid at plan time)


class RiskPlan(BaseModel):
    """Defined-risk plan. Every proposed trade MUST have bounded max loss."""

    max_loss_usd: float  # defined, worst-case dollar loss
    max_profit_usd: float | None = None  # None = uncapped (long option)
    breakeven: float | None = None
    account_risk_pct: float  # fraction of equity at risk in this trade
    reward_to_risk: float | None = None

    # Exit discipline
    profit_target_pct: float  # e.g. 0.5 = take profits at +50% of debit
    stop_loss_pct: float  # e.g. 0.5 = cut at -50% of debit
    time_stop_dte: int | None = None  # close if DTE falls below this
    invalidation_note: str = ""


class SpreadAnalytics(BaseModel):
    """Structure analytics computed at plan time from the legs + market state."""

    breakevens: list[float] = Field(default_factory=list)
    probability_of_profit: float | None = None  # [0, 1], risk-neutral estimate
    expected_value_usd: float | None = None  # POP-weighted EV (rough)
    net_delta: float | None = None  # position greeks (x100 x contracts)
    net_gamma: float | None = None
    net_theta: float | None = None  # $/day
    net_vega: float | None = None  # $ per 1 IV point
    is_credit: bool = False


class ExitLevel(BaseModel):
    """One concrete exit trigger with the net price to close at and its P&L."""

    kind: str  # take_profit | stop | time_stop
    label: str
    net_price: float | None  # per-share net price of the structure to close at
    pnl_usd: float | None  # position P&L (all contracts) if closed here
    note: str = ""


class ExitPlan(BaseModel):
    """Mechanical exit plan: concrete take-profit / stop / time-stop levels so a
    trade never needs a judgment call at management time."""

    method: str  # debit_vertical | credit_vertical | long_option
    action: str  # sell_to_close | buy_to_close
    entry_net_per_share: float  # debit (>0) or credit magnitude
    contracts: int
    max_profit_usd: float | None
    max_loss_usd: float
    breakevens: list[float] = Field(default_factory=list)
    time_stop_dte: int | None = None
    levels: list[ExitLevel] = Field(default_factory=list)


class TradePlan(BaseModel):
    """A concrete, sized, defined-risk expression of a thesis."""

    symbol: str
    direction: Direction
    strategy: StrategyType
    legs: list[ContractLeg] = Field(default_factory=list)
    net_debit: float  # per-1-lot net debit (negative = credit) in dollars
    contracts: int = Field(ge=1)
    risk: RiskPlan
    analytics: SpreadAnalytics | None = None
    exit_plan: ExitPlan | None = None
    rationale: str = ""

    @property
    def total_debit_usd(self) -> float:
        return round(self.net_debit * self.contracts, 2)


class OrderProposal(BaseModel):
    """A proposed order ticket requiring explicit human approval (Mode 3)."""

    id: str
    scan_id: str
    symbol: str
    status: ProposalStatus = ProposalStatus.DRAFT
    trade_plan: TradePlan
    thesis_summary: str
    created_at: datetime
    expires_at: datetime | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    reject_note: str | None = None


class PaperTrade(BaseModel):
    """A simulated position with MFE/MAE and slippage tracking (Mode 2)."""

    id: str
    scan_id: str
    symbol: str
    trade_plan: TradePlan
    status: PaperTradeStatus = PaperTradeStatus.OPEN

    opened_at: datetime
    entry_fill: float  # actual simulated fill (with slippage) per 1 lot
    entry_slippage: float = 0.0

    closed_at: datetime | None = None
    exit_fill: float | None = None
    exit_slippage: float = 0.0
    exit_reason: ExitReason | None = None

    # Excursion tracking (in dollars, position-level)
    mfe_usd: float = 0.0  # max favorable excursion
    mae_usd: float = 0.0  # max adverse excursion
    realized_pnl_usd: float | None = None

    def mark_pnl_usd(self, current_price: float) -> float:
        """Unrealized P&L in dollars at a given per-lot option price."""
        return round((current_price - self.entry_fill) * self.trade_plan.contracts * 100, 2)
