"""Paper engine: fills, MFE/MAE, and exit rules."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.domain.enums import Direction, ExitReason, OptionType
from app.domain.options import Greeks, OptionContract
from app.risk.policy import RiskPolicy
from app.risk.trade_plan import build_long_option_plan
from app.services.paper_engine import (
    check_exit,
    close_paper_trade,
    open_paper_trade,
    update_mark,
)


def _plan(policy: RiskPolicy):
    contract = OptionContract(
        symbol="AAA",
        expiration=date(2026, 8, 21),
        strike=100.0,
        option_type=OptionType.CALL,
        bid=0.10,
        ask=0.12,
        volume=500,
        open_interest=2000,
        greeks=Greeks(delta=0.45),
        as_of=datetime.now(UTC),
    )
    return build_long_option_plan(contract, Direction.BULLISH, policy, date(2026, 7, 16))


def test_open_applies_entry_slippage(policy: RiskPolicy) -> None:
    plan = _plan(policy)
    assert plan is not None
    trade = open_paper_trade(plan, "scan1", entry_mid=0.11, entry_spread=0.02)
    assert trade.entry_fill > 0.11  # buyer pays up
    assert trade.entry_slippage > 0


def test_mfe_mae_track_extremes(policy: RiskPolicy) -> None:
    plan = _plan(policy)
    assert plan is not None
    trade = open_paper_trade(plan, "scan1", entry_mid=0.11)
    update_mark(trade, 0.20)  # favorable
    update_mark(trade, 0.05)  # adverse
    update_mark(trade, 0.15)
    assert trade.mfe_usd > 0
    assert trade.mae_usd < 0


def test_profit_target_and_stop(policy: RiskPolicy) -> None:
    plan = _plan(policy)
    assert plan is not None
    trade = open_paper_trade(plan, "scan1", entry_mid=0.10)
    entry = trade.entry_fill
    assert check_exit(trade, entry * 1.6) == ExitReason.PROFIT_TARGET
    assert check_exit(trade, entry * 0.4) == ExitReason.STOP_LOSS
    assert check_exit(trade, entry * 1.0) is None


def test_close_computes_realized_pnl(policy: RiskPolicy) -> None:
    plan = _plan(policy)
    assert plan is not None
    trade = open_paper_trade(plan, "scan1", entry_mid=0.10)
    close_paper_trade(trade, exit_mid=0.20, reason=ExitReason.PROFIT_TARGET)
    assert trade.realized_pnl_usd is not None
    assert trade.closed_at is not None
