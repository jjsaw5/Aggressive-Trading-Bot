"""Flow-alpha experiment machinery: proxy features + gate, walk-forward
purge/embargo, and the bootstrap / grid statistics. Pure, no API.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.backtest.flow_experiment import ExpTrade, pooled_confirm_minus_oppose, run_experiment
from app.backtest.flow_proxy import (
    FlowFeatures,
    FlowThresholds,
    aggregate_day,
    features,
    flow_arm,
)
from app.backtest.stats import (
    bonferroni_alpha,
    bootstrap_diff_ci,
    summarize_grid,
)
from app.backtest.walk_forward import walk_forward_folds
from app.domain.historic import HistoricOptionBar


def _fb(otype: str, *, ask, bid, sweep, vol, oi, prem) -> HistoricOptionBar:
    return HistoricOptionBar(
        contract_id=f"X260821{otype}00100000", date=date(2024, 1, 10),
        nbbo_bid=1.0, nbbo_ask=1.1, ask_volume=ask, bid_volume=bid,
        sweep_volume=sweep, volume=vol, open_interest=oi, total_premium=prem,
        option_type=otype,
    )


# --- flow proxy --------------------------------------------------------------
def test_aggregate_and_features() -> None:
    bars = [
        _fb("C", ask=200, bid=50, sweep=40, vol=120, oi=1000, prem=600),
        _fb("P", ask=100, bid=50, sweep=10, vol=80, oi=1000, prem=200),
    ]
    raw = aggregate_day(bars)
    assert raw.ask_vol == 300 and raw.bid_vol == 100
    assert raw.call_prem == 600 and raw.put_prem == 200 and raw.total_prem == 800

    d = date(2024, 1, 10)
    # trailing premium series with variance so premium_z is defined and > 0
    hist = {d - timedelta(days=i): _mkraw(300 + (i % 3) * 50) for i in range(1, 11)}
    hist[d] = raw
    f = features(hist, d)
    assert f is not None
    assert f.at_ask_lean == round((300 - 100) / 400, 4)  # 0.5
    assert f.sweep_frac == round(50 / 200, 4)  # 0.25
    assert f.net_call_put == round((600 - 200) / 800, 4)  # 0.5
    assert f.premium_z > 0  # 800 well above the trailing-400 mean
    assert f.bull_score == round(f.at_ask_lean + f.net_call_put, 4)


def _mkraw(prem):
    from app.backtest.flow_proxy import FlowRaw
    return FlowRaw(ask_vol=200, bid_vol=200, sweep_vol=10, volume=100, open_interest=1000,
                   call_prem=prem / 2, put_prem=prem / 2, total_prem=prem, n_contracts=4)


def test_flow_arm_confirm_oppose_neutral() -> None:
    bull = FlowFeatures(at_ask_lean=0.5, sweep_frac=0.25, premium_z=0.0, net_call_put=0.6, voloi=0.2)
    thr = FlowThresholds(lean=0.1, sweep=0.2, prem=1.0)
    assert flow_arm(bull, "bullish", thr) == "CONFIRM"
    assert flow_arm(bull, "bearish", thr) == "OPPOSE"
    # below the magnitude bar -> NEUTRAL regardless of direction
    weak = FlowFeatures(at_ask_lean=0.05, sweep_frac=0.05, premium_z=0.0, net_call_put=0.6, voloi=0.2)
    assert flow_arm(weak, "bullish", thr) == "NEUTRAL"
    assert flow_arm(None, "bullish", thr) == "NEUTRAL"  # no read -> never guessed


# --- walk-forward ------------------------------------------------------------
def test_walk_forward_purges_and_embargoes() -> None:
    # Monthly entries through 2021, each held 20 days.
    items = [(date(2021, m, 1), date(2021, m, 21)) for m in range(1, 13)]
    folds = walk_forward_folds(
        items, entry_of=lambda x: x[0], exit_of=lambda x: x[1],
        n_folds=2, embargo_days=10,
    )
    assert folds
    for f in folds:
        barrier = f.test_start - timedelta(days=10)
        # every train item is fully resolved on/before the embargoed barrier
        assert all(x[1] <= barrier for x in f.train)
        # every test item enters inside the segment
        assert all(f.test_start <= x[0] < f.test_end for x in f.test)


def test_walk_forward_reverse_direction() -> None:
    items = [(date(2021, m, 1), date(2021, m, 21)) for m in range(1, 13)]
    folds = walk_forward_folds(
        items, entry_of=lambda x: x[0], exit_of=lambda x: x[1],
        n_folds=2, embargo_days=10, reverse=True,
    )
    for f in folds:
        barrier = f.test_end + timedelta(days=10)
        assert all(x[0] >= barrier for x in f.train)  # train is strictly later


# --- stats -------------------------------------------------------------------
def test_bootstrap_ci_separates_signal_from_noise() -> None:
    sep = bootstrap_diff_ci([10.0] * 20, [0.0] * 20, iters=2000)
    assert sep is not None and sep.point == 10.0 and sep.excludes_zero
    null = bootstrap_diff_ci([1.0, -1.0] * 20, [1.0, -1.0] * 20, iters=2000)
    assert null is not None and not null.excludes_zero
    assert bootstrap_diff_ci([], [1.0]) is None


def test_grid_summary_reports_median_not_argmax() -> None:
    g = summarize_grid([-2.0, -1.0, 0.5, 1.0, 8.0])  # one big positive outlier
    assert g.median_lift == 0.5  # not 8.0
    assert g.frac_positive == round(3 / 5, 4)
    assert g.best_lift == 8.0
    assert bonferroni_alpha(0.05, 36) == 0.05 / 36


# --- orchestration: honest null on a no-signal sample ------------------------
def _confirm_feat() -> FlowFeatures:
    return FlowFeatures(at_ask_lean=0.5, sweep_frac=0.3, premium_z=0.0, net_call_put=0.6, voloi=0.2)


def _oppose_feat() -> FlowFeatures:
    return FlowFeatures(at_ask_lean=-0.5, sweep_frac=0.3, premium_z=0.0, net_call_put=-0.6, voloi=0.2)


def _no_signal_trades() -> list[ExpTrade]:
    # Same P&L distribution for CONFIRM and OPPOSE within one regime×direction cell
    # => the flow gate carries no information => the spread must be ~0.
    out = []
    base = date(2023, 1, 2)
    pnls = [40.0, -30.0, 50.0, -60.0, 20.0, -20.0]
    for i in range(60):
        feat = _confirm_feat() if i % 2 == 0 else _oppose_feat()
        # Pair (2k, 2k+1) share a P&L, so CONFIRM and OPPOSE see the SAME
        # distribution — arm carries no information.
        pnl = pnls[(i // 2) % len(pnls)]
        e = base + timedelta(days=i * 5)
        out.append(ExpTrade(entry_date=e, exit_date=e + timedelta(days=20),
                            direction="bullish", regime="recovery",
                            net_by_k={0.5: pnl + 5, 1.0: pnl}, flow=feat))
    return out


def test_within_cell_spread_and_null_verdict() -> None:
    trades = _no_signal_trades()
    thr = FlowThresholds(lean=0.1, sweep=0.2, prem=1.0)
    ci = pooled_confirm_minus_oppose(trades, thr, 1.0)
    assert ci is not None and not ci.excludes_zero  # no information -> CI spans zero

    grid = [FlowThresholds(lean=lo, sweep=0.2, prem=1.0) for lo in (0.1, 0.2)]
    res = run_experiment(trades, grid, n_folds=2, embargo_days=55, material_margin=15.0)
    assert res.verdict == "fail_to_reject_H0"
    assert any("CI does not exclude zero" in r or "insufficient sample" in r for r in res.reasons)


def test_stress_fold_reversal_fails() -> None:
    # CONFIRM beats OPPOSE in 2024 but REVERSES in 2025 (which holds the stress
    # window) -> the stress fold must fail the verdict with the drawdown reason.
    out = []
    base = date(2024, 1, 8)
    for i in range(120):
        e = base + timedelta(days=i * 5)  # ~2024-01 .. 2025-08
        arm_confirm = i % 2 == 0
        feat = _confirm_feat() if arm_confirm else _oppose_feat()
        pre_2025 = e < date(2025, 1, 1)
        # 2024: CONFIRM wins; 2025 (incl. stress): CONFIRM loses. Both arms carry losses.
        pnl = (40.0 if arm_confirm else -40.0) if pre_2025 else (-40.0 if arm_confirm else 40.0)
        out.append(ExpTrade(entry_date=e, exit_date=e + timedelta(days=20),
                            direction="bullish", regime="mixed",
                            net_by_k={0.5: pnl + 5, 1.0: pnl}, flow=feat))
    grid = [FlowThresholds(lean=0.1, sweep=0.2, prem=1.0)]
    res = run_experiment(out, grid, n_folds=2, embargo_days=55, material_margin=15.0,
                         stress_window=(date(2025, 2, 15), date(2025, 4, 30)))
    assert res.stress_fold_spread is not None  # trades existed in the drawdown window
    assert res.stress_confirm_n >= 3 and res.stress_oppose_n >= 3  # both arms present
    assert res.stress_fold_ok is False  # flow reversed in the selloff
    assert res.verdict == "fail_to_reject_H0"
    assert any("drawdown" in r for r in res.reasons)
