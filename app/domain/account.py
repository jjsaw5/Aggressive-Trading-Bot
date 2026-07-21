"""Account-state model — the capital picture that sizing reads.

Sizing must never size against a constant pretending to be the account. This
model carries the *real* capital picture (equity, buying power, already-committed
risk) plus a `verified` flag and a `source` so a decision is honest about whether
it was made against a live broker feed or an unverified paper/fallback estimate.
Nothing here becomes live-executable from a constant: paper and configured-fallback
are always `verified=False`.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class AccountState(BaseModel):
    """A point-in-time capital snapshot for position sizing."""

    source: str  # "paper" | "live" | "fallback"
    verified: bool  # True ONLY for a real, authenticated broker feed
    equity_usd: float
    buying_power_usd: float
    open_risk_usd: float = 0.0  # defined worst-case risk across OPEN positions
    pending_risk_usd: float = 0.0  # defined risk of pending / working orders
    cash_usd: float | None = None
    as_of: datetime
    note: str = ""

    @property
    def committed_risk_usd(self) -> float:
        """Dollars already at risk (open + pending)."""
        return round(self.open_risk_usd + self.pending_risk_usd, 2)

    @property
    def available_risk_usd(self) -> float:
        """Risk budget a NEW trade may draw on: equity minus what is already
        committed, floored at zero and never exceeding buying power."""
        free = self.equity_usd - self.committed_risk_usd
        return round(max(0.0, min(free, self.buying_power_usd)), 2)
