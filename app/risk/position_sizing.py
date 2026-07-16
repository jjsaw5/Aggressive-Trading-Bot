"""Position sizing for defined-risk options trades.

The core rule: a single trade may never risk more than
`policy.max_trade_risk_usd` (the tighter of % of equity and the absolute
per-trade dollar cap). For a small account this is the difference between
survivable losers and account-ending ones.

`size_by_defined_risk` returns the number of contracts (possibly 0) such that
worst-case loss stays within the per-trade cap, along with the resulting
defined risk. It NEVER rounds up past the cap.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.risk.policy import RiskPolicy


@dataclass(frozen=True)
class SizingResult:
    contracts: int
    max_loss_usd: float
    per_contract_risk_usd: float
    account_risk_pct: float
    capped_reason: str | None = None

    @property
    def is_tradeable(self) -> bool:
        return self.contracts >= 1


def size_by_defined_risk(
    per_contract_risk_usd: float,
    policy: RiskPolicy,
    *,
    open_risk_usd: float = 0.0,
) -> SizingResult:
    """Size a trade whose per-contract worst-case loss is known.

    Args:
        per_contract_risk_usd: Max loss for ONE contract/spread in dollars.
            For a long option this is `mid * 100`. For a defined-risk spread it
            is `(width - credit) * 100` or `debit * 100`.
        policy: Active risk policy.
        open_risk_usd: Dollars already at risk across open positions, so a new
            trade cannot breach the account-level cap.
    """
    if per_contract_risk_usd <= 0:
        return SizingResult(0, 0.0, per_contract_risk_usd, 0.0, "invalid_per_contract_risk")

    per_trade_cap = policy.max_trade_risk_usd
    remaining_account = max(0.0, policy.max_account_risk_usd - open_risk_usd)
    effective_cap = min(per_trade_cap, remaining_account)

    if effective_cap < per_contract_risk_usd:
        reason = (
            "account_cap_exhausted"
            if remaining_account < per_trade_cap
            else "single_contract_exceeds_trade_cap"
        )
        return SizingResult(0, 0.0, per_contract_risk_usd, 0.0, reason)

    contracts = int(effective_cap // per_contract_risk_usd)
    capped = None
    if contracts * per_contract_risk_usd + per_contract_risk_usd > effective_cap:
        capped = "risk_cap"

    # Concentration / fill-risk cap: never size beyond a sane contract count,
    # regardless of how cheap the per-contract risk is.
    if contracts > policy.max_contracts_per_trade:
        contracts = policy.max_contracts_per_trade
        capped = "contract_count_cap"

    max_loss = round(contracts * per_contract_risk_usd, 2)
    account_pct = round(max_loss / policy.account_equity_usd, 4)
    return SizingResult(contracts, max_loss, per_contract_risk_usd, account_pct, capped)
