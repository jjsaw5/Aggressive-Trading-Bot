"""Paper-trading engine (Mode 2).

Simulates fills with a configurable slippage model, tracks maximum favorable
excursion (MFE) and maximum adverse excursion (MAE), and applies the trade
plan's exit rules (profit target / stop / time stop). No external orders.

The engine is pure and deterministic given its inputs so it can also power
backtests over historical option marks.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain.enums import ExitReason, PaperTradeStatus
from app.domain.trades import PaperTrade, TradePlan


@dataclass(frozen=True)
class SlippageModel:
    """Simple slippage: pay a fraction of the bid/ask spread plus a floor."""

    spread_fraction: float = 0.5  # cross half the spread by default
    min_slippage_per_share: float = 0.01

    def entry_fill(self, mid: float, spread: float) -> float:
        # `mid` is the SIGNED net (debit > 0, credit < 0). Slippage always
        # worsens the entry: +slip moves a debit up (pay more) and a credit
        # toward zero (receive less), so this is correct for both.
        slip = max(self.min_slippage_per_share, spread * self.spread_fraction / 2)
        return round(mid + slip, 4)

    def exit_fill(self, mid: float, spread: float) -> float:
        # Closing always worsens: -slip lowers a debit sale and pushes a credit
        # buy-back further negative. Correct for both signs; no zero clamp (a
        # credit structure's net is legitimately negative).
        slip = max(self.min_slippage_per_share, spread * self.spread_fraction / 2)
        return round(mid - slip, 4)


def open_paper_trade(
    plan: TradePlan,
    scan_id: str,
    *,
    entry_mid: float,
    entry_spread: float = 0.0,
    slippage: SlippageModel | None = None,
    now: datetime | None = None,
) -> PaperTrade:
    slippage = slippage or SlippageModel()
    now = now or datetime.now(UTC)
    fill = slippage.entry_fill(entry_mid, entry_spread)
    return PaperTrade(
        id=uuid.uuid4().hex[:12],
        scan_id=scan_id,
        symbol=plan.symbol,
        trade_plan=plan,
        status=PaperTradeStatus.OPEN,
        opened_at=now,
        entry_fill=fill,
        entry_slippage=round(fill - entry_mid, 4),
    )


def update_mark(trade: PaperTrade, current_mid: float) -> PaperTrade:
    """Update MFE/MAE from a new option mark. Returns the same instance."""
    pnl = trade.mark_pnl_usd(current_mid)
    trade.mfe_usd = round(max(trade.mfe_usd, pnl), 2)
    trade.mae_usd = round(min(trade.mae_usd, pnl), 2)
    return trade


def check_exit(trade: PaperTrade, current_mid: float) -> ExitReason | None:
    """Evaluate the plan's exit rules against the current (signed net) mark.

    `change` is P&L as a fraction of the capital at stake — debit paid or credit
    received — via `abs(entry_fill)` in the denominator. So a +50% profit target
    means "captured 50% of the debit" for a long and "captured 50% of the
    credit" for a short, and the sign of (current - entry) is P&L direction for
    both. This makes exits correct for credit spreads and iron condors too.
    """
    entry = trade.entry_fill
    if abs(entry) < 1e-6:
        return None
    change = (current_mid - entry) / abs(entry)
    risk = trade.trade_plan.risk
    if change >= risk.profit_target_pct:
        return ExitReason.PROFIT_TARGET
    if change <= -risk.stop_loss_pct:
        return ExitReason.STOP_LOSS
    return None


def close_paper_trade(
    trade: PaperTrade,
    *,
    exit_mid: float,
    exit_spread: float = 0.0,
    reason: ExitReason,
    slippage: SlippageModel | None = None,
    now: datetime | None = None,
) -> PaperTrade:
    slippage = slippage or SlippageModel()
    now = now or datetime.now(UTC)
    fill = slippage.exit_fill(exit_mid, exit_spread)
    trade.status = PaperTradeStatus.CLOSED
    trade.closed_at = now
    trade.exit_fill = fill
    trade.exit_slippage = round(exit_mid - fill, 4)
    trade.exit_reason = reason
    trade.realized_pnl_usd = round(
        (fill - trade.entry_fill) * trade.trade_plan.contracts * 100, 2
    )
    return trade
