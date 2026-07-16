"""Pure per-trade backtest engine.

Steps a sized `TradePlan` along a daily underlying price path, repricing the
position with Black-Scholes each day, tracking MFE/MAE, and applying the plan's
exit discipline (profit target, stop, time stop, expiry). Reuses the paper
engine so simulated and backtested trades share identical accounting.

Pure and deterministic given its inputs — no I/O, no randomness — so results
are exactly reproducible and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.enums import ExitReason
from app.domain.trades import PaperTrade, TradePlan
from app.quant.pricing import net_position_price, plan_entry_net_per_share
from app.services.paper_engine import (
    SlippageModel,
    check_exit,
    close_paper_trade,
    open_paper_trade,
    update_mark,
)


@dataclass
class BacktestResult:
    trade: PaperTrade
    entry_dte: int
    days_held: int
    exit_reason: ExitReason
    trajectory: list[float] = field(default_factory=list)  # daily net per-share marks

    @property
    def realized_pnl_usd(self) -> float:
        return self.trade.realized_pnl_usd or 0.0

    @property
    def is_win(self) -> bool:
        return self.realized_pnl_usd > 0


def backtest_trade(
    plan: TradePlan,
    entry_dte: int,
    path_spots: list[float],
    vol: float,
    *,
    scan_id: str = "backtest",
    rate: float = 0.04,
    slippage: SlippageModel | None = None,
    reprice_entry: bool = False,
) -> BacktestResult:
    """Backtest one plan over a daily spot path (index 0 = entry day).

    Args:
        plan: the sized, defined-risk plan.
        entry_dte: days-to-expiry of the plan at entry.
        path_spots: daily underlying spot prices starting at entry day.
        vol: implied volatility used to reprice legs (held constant).
        reprice_entry: if True, set the entry mark by Black-Scholes at the path's
            starting spot instead of the plan's stored leg prices. This makes the
            entry lie on the SAME pricing curve used to reprice the path, which is
            essential for an unbiased simulation — otherwise a mismatch between
            stored entry prices and the model injects a spurious edge.
    """
    slippage = slippage or SlippageModel()
    # Signed net: debit > 0, credit < 0. No zero clamp — a credit structure's
    # net is legitimately negative and must stay so for correct P&L.
    if reprice_entry and path_spots:
        entry_net = net_position_price(plan, path_spots[0], entry_dte, vol, rate)
    else:
        entry_net = plan_entry_net_per_share(plan)
    trade = open_paper_trade(plan, scan_id, entry_mid=entry_net, entry_spread=0.0, slippage=slippage)

    trajectory: list[float] = []
    exit_reason: ExitReason | None = None
    days_held = 0

    for day, spot in enumerate(path_spots):
        dte = entry_dte - day
        net = net_position_price(plan, spot, dte, vol, rate)
        trajectory.append(net)

        if day == 0:
            continue  # entry day already accounted for at fill

        update_mark(trade, net)
        days_held = day

        reason = check_exit(trade, net)
        if reason is None and dte <= plan.risk.time_stop_dte:
            reason = ExitReason.TIME_STOP
        if reason is None and dte <= 0:
            reason = ExitReason.EXPIRY
        if reason is not None:
            exit_reason = reason
            close_paper_trade(trade, exit_mid=net, reason=reason, slippage=slippage)
            break

    if exit_reason is None:
        # Ran out of price data before an exit fired — close at last mark.
        last_net = trajectory[-1] if trajectory else entry_net
        last_dte = entry_dte - (len(path_spots) - 1)
        exit_reason = ExitReason.EXPIRY if last_dte <= 0 else ExitReason.MANUAL
        days_held = max(0, len(path_spots) - 1)
        close_paper_trade(trade, exit_mid=last_net, reason=exit_reason, slippage=slippage)

    return BacktestResult(
        trade=trade,
        entry_dte=entry_dte,
        days_held=days_held,
        exit_reason=exit_reason,
        trajectory=trajectory,
    )
