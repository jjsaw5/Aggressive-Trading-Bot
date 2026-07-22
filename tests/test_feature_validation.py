"""Feature-validation harness — Spearman, walk-forward OOS, per-regime, registry."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from app.backtest.feature_validation import build_registry, validate_feature
from app.backtest.features import FeatureVector
from app.backtest.stats import bootstrap_corr_ci, spearman


def test_spearman_basic() -> None:
    assert spearman([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)
    # Constant column -> undefined, not a crash.
    assert spearman([1, 1, 1, 1], [1, 2, 3, 4]) is None
    assert spearman([1, 2], [1, 2]) is None  # too few points


def test_bootstrap_corr_ci_detects_signal_and_null() -> None:
    xs = [float(i) for i in range(60)]
    ys = [float(i) + (i % 3) for i in range(60)]  # strongly monotone
    ci = bootstrap_corr_ci(xs, ys)
    assert ci is not None and ci.lo > 0 and ci.excludes_zero
    # A shuffled-independent pair should not reliably exclude zero.
    ind = [float((i * 37) % 60) for i in range(60)]
    ci2 = bootstrap_corr_ci([float(i) for i in range(60)], ind)
    assert ci2 is not None and not ci2.excludes_zero


def _fv(i: int, feat_val: float, net: float, regime: str = "mid", entry: date | None = None) -> FeatureVector:
    entry = entry or (date(2023, 1, 1) + timedelta(days=i * 3))
    return FeatureVector(
        trade_id=f"t{i}", entry_date=entry, exit_date=entry + timedelta(days=10),
        vol_regime=regime, net_pnl=net, values={"dte": feat_val, "structure": "call_debit_spread"},
    )


def test_validate_feature_flags_real_signal() -> None:
    # A numeric feature (dte here as a stand-in) that genuinely tracks net P&L, same
    # sign in every regime, should validate out-of-sample.
    items = []
    for i in range(120):
        fval = float(i % 40)
        net = (fval - 20) * 10.0  # monotone in the feature
        regime = ["low", "mid", "high"][i % 3]
        items.append(_fv(i, fval, net, regime))
    v = validate_feature(items, "dte", n_folds=4, embargo_days=7, alpha=0.05, n_features=5)
    assert v.n_oos >= 30
    assert v.validated is True
    assert v.oos_rho is not None and v.oos_rho > 0
    assert v.regime_sign_flip is False


def test_validate_feature_rejects_regime_flipping_beta() -> None:
    # A feature whose relationship to net P&L FLIPS sign by regime is conditional
    # beta, not robust edge — must not validate even if it "works" in aggregate.
    items = []
    for i in range(150):
        fval = float(i % 30)
        regime = ["low", "high"][i % 2]
        # positive slope in low-vol, negative in high-vol
        net = (fval - 15) * (12.0 if regime == "low" else -12.0)
        items.append(_fv(i, fval, net, regime))
    v = validate_feature(items, "dte", n_folds=4, embargo_days=7, alpha=0.05, n_features=5)
    assert v.regime_sign_flip is True
    assert v.validated is False


def test_build_registry_empty_when_nothing_validates() -> None:
    # Pure noise features -> no weights, honest null (Layer-1 UNCALIBRATED stays).
    # Label is seeded-random and independent of the feature, so no OOS association.
    rng = random.Random(7)
    items = []
    for i in range(150):
        fval = float(i % 40)
        net = rng.uniform(-100, 100)  # independent of fval
        items.append(_fv(i, fval, net, ["low", "mid", "high"][i % 3]))
    reg = build_registry(items, features=("dte",), n_folds=4, embargo_days=7)
    assert reg.any_validated is False
    assert reg.weights == {}
    assert reg.as_dict()["any_validated"] is False


def test_registry_weights_sum_to_100_when_validated() -> None:
    items = []
    for i in range(120):
        fval = float(i % 40)
        net = (fval - 20) * 10.0
        items.append(_fv(i, fval, net, ["low", "mid", "high"][i % 3]))
    reg = build_registry(items, features=("dte",), n_folds=4, embargo_days=7)
    if reg.any_validated:
        assert abs(sum(reg.weights.values()) - 100.0) < 0.5
