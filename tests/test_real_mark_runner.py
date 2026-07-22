"""Aggregation of real-mark trade evaluations into a validating report.

Pure over fabricated results (no API): pins the cost-net dual-k roll-up, the
per-structure / per-vol-regime grouping, slippage-fragile counting, exclusion
tallying, and the degeneracy guard that keeps a no-loss / single-regime sample
from reading as validation.
"""

from __future__ import annotations

from datetime import date

from app.backtest.fill_model import RoundTrip
from app.backtest.real_mark import RealMarkResult, RealMarkTrade
from app.backtest.real_mark_runner import aggregate


def _trade(tid: str, *, strategy: str, regime: str) -> RealMarkTrade:
    return RealMarkTrade(
        trade_id=tid, long_id="L", short_id="S", entry_date=date(2026, 1, 1),
        dte_at_entry=30, strategy=strategy, vol_regime=regime,
    )


def _result(
    tid: str, k_pnls: dict[float, float], *, exit_day: int,
    included: bool = True, exclusion: str | None = None, fragile: bool = False,
) -> RealMarkResult:
    by_k = {
        k: RoundTrip(True, 1.0, 1.0, pnl + 2.6, 2.6, pnl, k) for k, pnl in k_pnls.items()
    }
    return RealMarkResult(
        trade_id=tid, included=included, exclusion_reason=exclusion,
        entry_date=date(2026, 1, 1),
        exit_date=date(2026, 1, 1 + exit_day) if included else None,
        days_held=exit_day, exit_reason="time_stop" if included else None,
        by_k=by_k if included else {}, horizon="swing", fidelity="VALIDATING",
        slippage_fragile=fragile,
    )


def test_aggregate_rolls_up_dual_k_and_groups() -> None:
    pairs = [
        (_trade("a", strategy="call_debit_spread", regime="low"),
         _result("a", {0.5: 120, 1.0: 100}, exit_day=1)),
        (_trade("b", strategy="call_debit_spread", regime="low"),
         _result("b", {0.5: -60, 1.0: -80}, exit_day=2)),
        (_trade("c", strategy="put_credit_spread", regime="high"),
         _result("c", {0.5: 70, 1.0: 60}, exit_day=3, fragile=True)),
        (_trade("d", strategy="put_debit_spread", regime="high"),
         _result("d", {0.5: -40, 1.0: -50}, exit_day=4)),
    ]
    rep = aggregate(pairs)

    assert rep.n_trades == 4 and rep.n_included == 4 and rep.n_excluded == 0
    # Conservative k costs more than optimistic k.
    assert rep.by_k[1.0].net_pnl_usd == 100 - 80 + 60 - 50  # 30
    assert rep.by_k[0.5].net_pnl_usd == 120 - 60 + 70 - 40  # 90
    assert rep.by_k[1.0].wins == 2 and rep.by_k[1.0].losses == 2
    assert rep.slippage_fragile_n == 1
    # Winners span two regimes and there are >=2 losses -> a gradeable sample.
    assert rep.warnings == []
    structures = {g.key: g.net_pnl_usd for g in rep.by_structure}
    assert structures["call_debit_spread"] == 20  # 100 - 80
    assert {g.key for g in rep.by_vol_regime} == {"low", "high"}


def test_aggregate_flags_no_loss_single_regime() -> None:
    pairs = [
        (_trade(f"w{i}", strategy="put_credit_spread", regime="high"),
         _result(f"w{i}", {0.5: 50 + i, 1.0: 40 + i}, exit_day=i + 1))
        for i in range(3)
    ]
    rep = aggregate(pairs)
    assert any("loss" in w for w in rep.warnings)
    assert any("single vol regime" in w for w in rep.warnings)


def test_aggregate_tallies_exclusions() -> None:
    pairs = [
        (_trade("a", strategy="call_debit_spread", regime="low"),
         _result("a", {1.0: 10}, exit_day=1)),
        (_trade("x", strategy="call_debit_spread", regime="low"),
         _result("x", {}, exit_day=0, included=False, exclusion="entry_liquidity_guard")),
        (_trade("y", strategy="put_debit_spread", regime="low"),
         _result("y", {}, exit_day=0, included=False, exclusion="entry_liquidity_guard")),
    ]
    rep = aggregate(pairs)
    assert rep.n_excluded == 2
    assert rep.exclusions == {"entry_liquidity_guard": 2}
    assert rep.by_k[1.0].n == 1  # only the fillable, included trade
