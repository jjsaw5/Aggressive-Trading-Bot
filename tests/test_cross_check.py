"""Forward-ledger vs real-mark cross-check (pure, no DB/API).

The value is disagreement detection: a trade the ledger booked as a win that real
marks reprice as a loss (a sign flip) must be surfaced, because that is the unpaid
short-vol tail becoming visible. These tests pin the flip case, an agreeing case,
and the honest "can't reprice" fallbacks.
"""

from __future__ import annotations

from datetime import date

from app.backtest.cross_check import (
    LedgerTrade,
    cross_check_trade,
    summarize,
)
from app.domain.historic import HistoricOptionBar


def _bar(cid: str, d: date, bid: float, ask: float) -> HistoricOptionBar:
    return HistoricOptionBar(cid, d, bid, ask, open_interest=5000, volume=500)


def _put_credit_bars(entry_mid_long, entry_mid_short, exit_mid_long, exit_mid_short):
    """Bull put credit spread: long lower put (L), short higher put (S)."""
    e, x = date(2024, 1, 5), date(2024, 1, 25)
    hl = 0.05
    L = [_bar("L", e, entry_mid_long - hl, entry_mid_long + hl),
         _bar("L", x, exit_mid_long - hl, exit_mid_long + hl)]
    S = [_bar("S", e, entry_mid_short - hl, entry_mid_short + hl),
         _bar("S", x, exit_mid_short - hl, exit_mid_short + hl)]
    return L, S


def _trade(recorded_pnl, outcome="win") -> LedgerTrade:
    return LedgerTrade(
        ref="rec1", symbol="AAA", strategy="put_credit_spread",
        entry_date=date(2024, 1, 5), resolved_date=date(2024, 1, 25),
        long_id="L", short_id="S", contracts=1,
        recorded_net_pnl=recorded_pnl, recorded_outcome=outcome,
    )


def test_sign_flip_is_surfaced() -> None:
    # Ledger booked a +$120 WIN. But on real marks the spread WIDENED against us
    # (short leg richened more than the long), so the reprice is a loss.
    # entry credit: long 1.00 / short 2.00 -> net -1.00 (credit received).
    # exit: long 1.50 / short 4.00 -> liquidation sell_long - buy_short is worse.
    L, S = _put_credit_bars(1.00, 2.00, 1.50, 4.00)
    row = cross_check_trade(_trade(120.0, "win"), L, S)
    assert row.repriced
    assert row.recorded_result == "win"
    assert row.real_mark_result == "loss"
    assert row.sign_flip is True
    assert row.agree is False
    assert row.pnl_gap_usd is not None and row.pnl_gap_usd > 0  # ledger over-credited


def test_agreement_when_both_win() -> None:
    # Credit spread that decayed in our favor: short leg cheapens -> real-mark win.
    L, S = _put_credit_bars(1.00, 2.00, 0.40, 0.60)
    row = cross_check_trade(_trade(90.0, "win"), L, S)
    assert row.repriced and row.real_mark_result == "win"
    assert row.agree is True and row.sign_flip is False


def test_unrepriceable_when_marks_missing() -> None:
    t = _trade(50.0)
    # Only an entry bar exists; no bar on/before the resolution date for the short.
    L = [_bar("L", date(2024, 1, 5), 0.95, 1.05), _bar("L", date(2024, 1, 25), 0.35, 0.45)]
    S = [_bar("S", date(2024, 1, 5), 1.95, 2.05)]  # nothing near resolution
    row = cross_check_trade(t, L, S)
    assert row.repriced is False
    assert "real marks" in row.note


def test_summary_counts_flips() -> None:
    flip = cross_check_trade(_trade(120.0, "win"), *_put_credit_bars(1.0, 2.0, 1.5, 4.0))
    agree = cross_check_trade(_trade(90.0, "win"), *_put_credit_bars(1.0, 2.0, 0.4, 0.6))
    s = summarize([flip, agree])
    assert s.n == 2 and s.n_repriced == 2
    assert s.n_sign_flip == 1 and s.n_agree == 1
    assert len(s.flips) == 1 and "real-mark loss" in s.flips[0]
