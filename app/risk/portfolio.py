"""Account-level (portfolio) risk accounting.

Answers: *how does the proposed trade affect total account risk?* Tracks open
defined risk ("heat") and enforces the max concurrent positions + max aggregate
account risk limits before a new trade can be admitted.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import RejectReason
from app.risk.policy import RiskPolicy


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    defined_risk_usd: float


@dataclass(frozen=True)
class PortfolioState:
    positions: list[OpenPosition]

    @property
    def open_count(self) -> int:
        return len(self.positions)

    @property
    def open_risk_usd(self) -> float:
        return round(sum(p.defined_risk_usd for p in self.positions), 2)


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    reasons: list[RejectReason]
    projected_account_risk_pct: float
    open_risk_usd: float


def evaluate_admission(
    new_trade_risk_usd: float,
    portfolio: PortfolioState,
    policy: RiskPolicy,
) -> AdmissionDecision:
    reasons: list[RejectReason] = []

    if portfolio.open_count >= policy.max_concurrent_positions:
        reasons.append(RejectReason.PORTFOLIO_LIMIT)

    projected = portfolio.open_risk_usd + new_trade_risk_usd
    if projected > policy.max_account_risk_usd + 1e-6:
        reasons.append(RejectReason.RISK_UNMANAGEABLE)

    projected_pct = round(projected / policy.account_equity_usd, 4)
    return AdmissionDecision(
        admitted=not reasons,
        reasons=reasons,
        projected_account_risk_pct=projected_pct,
        open_risk_usd=portfolio.open_risk_usd,
    )
