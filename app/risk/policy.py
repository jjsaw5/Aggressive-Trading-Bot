"""Risk policy — the single source of truth for capital-preservation limits.

Defaults are sourced from settings (which default to a $2,000 account). Every
sizing and portfolio decision reads this policy; nothing hardcodes limits.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.config import settings


class RiskPolicy(BaseModel):
    account_equity_usd: float = Field(gt=0)
    max_account_risk_pct: float = Field(gt=0, le=1)
    max_trade_risk_pct: float = Field(gt=0, le=1)
    max_concurrent_positions: int = Field(ge=1)
    max_defined_risk_per_trade_usd: float = Field(gt=0)
    max_contracts_per_trade: int = Field(default=20, ge=1)

    # Exit discipline defaults (long-premium). Aggressive but rule-based.
    default_profit_target_pct: float = 0.50  # take profit at +50% of debit
    default_stop_loss_pct: float = 0.50  # cut loss at -50% of debit
    default_time_stop_dte: int = 7  # avoid theta cliff / gamma risk into expiry

    @property
    def max_trade_risk_usd(self) -> float:
        """Per-trade risk cap = min(% of equity, absolute $ cap)."""
        pct_cap = self.account_equity_usd * self.max_trade_risk_pct
        return round(min(pct_cap, self.max_defined_risk_per_trade_usd), 2)

    @property
    def max_account_risk_usd(self) -> float:
        return round(self.account_equity_usd * self.max_account_risk_pct, 2)

    @classmethod
    def from_settings(cls) -> RiskPolicy:
        return cls(
            account_equity_usd=settings.account_equity_usd,
            max_account_risk_pct=settings.max_account_risk_pct,
            max_trade_risk_pct=settings.max_trade_risk_pct,
            max_concurrent_positions=settings.max_concurrent_positions,
            max_defined_risk_per_trade_usd=settings.max_defined_risk_per_trade_usd,
            max_contracts_per_trade=settings.max_contracts_per_trade,
        )
