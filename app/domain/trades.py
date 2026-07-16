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


class TradePlan(BaseModel):
    """A concrete, sized, defined-risk expression of a thesis."""

    symbol: str
    direction: Direction
    strategy: StrategyType
    legs: list[ContractLeg] = Field(default_factory=list)
    net_debit: float  # per-1-lot net debit (negative = credit) in dollars
    contracts: int = Field(ge=1)
    risk: RiskPlan
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
