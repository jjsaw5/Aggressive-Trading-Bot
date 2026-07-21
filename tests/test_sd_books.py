"""Phase 4 — Book A (signal-validation) / Book B (account-executable) split."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.account import AccountState
from app.domain.enums import Direction, DTECategory, StrategyType
from app.domain.shortduration import ShortDurationTrade
from app.domain.trades import RiskPlan, TradePlan

_NOW = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)


def _plan(max_loss: float, contracts: int = 1) -> TradePlan:
    risk = RiskPlan(max_loss_usd=max_loss, account_risk_pct=0.03,
                    profit_target_pct=0.5, stop_loss_pct=0.5)
    return TradePlan(symbol="SPY", direction=Direction.BULLISH, strategy=StrategyType.LONG_CALL,
                     legs=[], net_debit=max_loss, contracts=contracts, risk=risk)


def _trade(pnl, executable, *, symbol="SPY", reason="") -> ShortDurationTrade:
    return ShortDurationTrade(
        id=f"t{abs(hash((pnl, executable, symbol, reason))) % 10**8}", candidate_id="c",
        paper_trade_id="p", symbol=symbol, dte_category=DTECategory.ZERO_DTE, opened_at=_NOW,
        status="closed", realized_pnl_usd=pnl, executable_at_entry=executable,
        not_executable_reason=reason,
    )


def _account(equity=2000.0, committed=0.0) -> AccountState:
    return AccountState(source="paper", verified=False, equity_usd=equity,
                        buying_power_usd=equity - committed, open_risk_usd=committed, as_of=_NOW)


def test_trade_book_property() -> None:
    assert _trade(10.0, True).book == "B"
    assert _trade(10.0, False).book == "A-only"


async def test_executable_at_entry_fits_account(monkeypatch) -> None:
    from app.shortduration import paper

    monkeypatch.setattr(paper, "_account_state_for_paper", lambda now: _fake(_account()))
    ok, why = await paper._executable_at_entry(_plan(50.0), DTECategory.ZERO_DTE, _NOW)
    assert ok is True and why == ""


async def test_executable_at_entry_too_big_for_account(monkeypatch) -> None:
    from app.shortduration import paper

    monkeypatch.setattr(paper, "_account_state_for_paper", lambda now: _fake(_account()))
    ok, why = await paper._executable_at_entry(_plan(500.0), DTECategory.ZERO_DTE, _NOW)
    assert ok is False and why  # a human reason, not empty


async def test_executable_check_forces_real_caps_even_in_unconstrained(monkeypatch) -> None:
    # Paper-unconstrained mode lifts caps for the signal book; the Book-B check must
    # still measure against the REAL account.
    from app.config import settings
    from app.shortduration import paper

    monkeypatch.setattr(settings, "short_duration_paper_unconstrained", True)
    monkeypatch.setattr(paper, "_account_state_for_paper", lambda now: _fake(_account()))
    ok, _ = await paper._executable_at_entry(_plan(500.0), DTECategory.ZERO_DTE, _NOW)
    assert ok is False  # not executable for the $2k account despite unconstrained paper mode


def test_opportunity_loss_math() -> None:
    from app.shortduration.paper import _opportunity_loss

    trades = [
        _trade(100.0, True), _trade(-50.0, True),          # Book B: +50
        _trade(200.0, False, reason="cap"), _trade(-30.0, False, reason="cap"),  # A-only: +170
    ]
    ol = _opportunity_loss(trades)
    assert ol["signals_decided"] == 4 and ol["executable_decided"] == 2
    assert ol["book_a_total_pnl"] == 220.0 and ol["book_b_total_pnl"] == 50.0
    assert ol["left_on_table_pnl"] == 170.0
    assert ol["top_missed"][0]["pnl_usd"] == 200.0
    assert ol["not_executable_reasons"] == {"cap": 2}


def test_performance_book_filter_shapes() -> None:
    from app.shortduration.paper import short_duration_performance

    both = short_duration_performance()
    # Default keeps the flat Book-A shape (backward compatible) + the split.
    assert {"overall", "by_dte", "book_b", "opportunity_loss", "open_positions"} <= both.keys()
    a = short_duration_performance("A")
    b = short_duration_performance("B")
    assert a["book"] == "A" and b["book"] == "B"
    # Book B is a subset of Book A, so it can never have more decided trades.
    assert b["overall"].get("trades", 0) <= a["overall"].get("trades", 0)


async def test_open_paper_trade_records_executability() -> None:
    from app.domain.enums import CandidateState
    from app.shortduration.detection import run_detection
    from app.shortduration.paper import open_short_duration_paper

    cands = await run_detection(DTECategory.SHORT_DTE, now=_NOW)
    tradeable = next(
        (c for c in cands if c.trade_plan is not None and c.state != CandidateState.REJECTED), None
    )
    if tradeable is None:
        pytest.skip("no tradeable candidate produced")
    trade = await open_short_duration_paper(tradeable, now=_NOW)
    try:
        assert isinstance(trade.executable_at_entry, bool)
        assert trade.book in {"B", "A-only"}
    finally:
        # Don't leak an open position into the shared DB — later scans size against it.
        from app.db.models import ShortDurationTradeRow
        from app.db.session import SessionLocal

        with SessionLocal() as s:
            row = s.get(ShortDurationTradeRow, trade.id)
            if row is not None:
                s.delete(row)
                s.commit()


async def _fake(state):
    return state
