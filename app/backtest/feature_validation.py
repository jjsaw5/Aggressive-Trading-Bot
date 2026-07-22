"""Feature-validation harness + registry (Conviction-Scanner spec §3/§4).

The original sin was hand-set weights: a composite that scored template-fit and
called it conviction (in-sample Spearman −0.68 against real outcomes). This module
is the antidote. For each candidate entry-time feature it asks one question with a
falsifiable answer:

    Does this feature predict a NET-OF-COST outcome OUT-OF-SAMPLE?

Method (leak-safe by construction):
  * Walk-forward folds with purge + embargo (overlapping option lifetimes leak
    under a naive split), run BOTH directions for a same-sign robustness check.
  * The feature's predictor is fit on TRAIN only — numeric features are oriented
    by the sign of their train correlation; categorical features are target-encoded
    on train means — then scored on the held-out TEST fold. No test row ever
    informs its own predictor.
  * Pool the oriented (predictor, outcome) pairs across all test folds and take the
    out-of-sample Spearman rho, with a seeded bootstrap CI.
  * Bonferroni-correct the CI over the number of features tested (no fishing).
  * Break the OOS association down PER VOL REGIME — a feature whose sign flips
    across regimes is regime-conditional beta, not robust edge, and is NOT
    validated even if the pooled CI clears zero.

A feature validates only if its Bonferroni-corrected OOS CI excludes zero on the
positive side AND its per-regime signs do not flip. Validated features receive a
data-derived weight proportional to their OOS effect size (|rho|), normalized to
sum to 100 — nothing hand-set. If nothing validates, the registry is empty and the
honest verdict is "no feature has earned conviction," which keeps Layer-1's
UNCALIBRATED degrade in force.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.backtest.features import (
    ALL_FEATURES,
    FLOW_FEATURES,
    NUMERIC_FEATURES,
    FeatureVector,
    exit_or_entry,
)
from app.backtest.stats import bonferroni_alpha, bootstrap_corr_ci, spearman
from app.backtest.walk_forward import walk_forward_folds

# Flow features are continuous, so they rank-correlate like the other numerics.
_NUMERIC = set(NUMERIC_FEATURES) | set(FLOW_FEATURES)

_MIN_FOLD_TEST = 8  # a test fold smaller than this can't estimate an OOS association
_MIN_POOLED = 30  # pooled OOS pairs below this → underpowered, report but don't validate


def _numeric_pairs(train: list[FeatureVector], test: list[FeatureVector], feat: str):
    """Oriented (predictor, outcome) test pairs for a numeric feature. Orientation is
    the sign of the TRAIN correlation, so a real relationship reads positive OOS."""
    tr = [(fv.values[feat], fv.net_pnl) for fv in train if fv.values.get(feat) is not None]
    if len(tr) < 5:
        return []
    sign_rho = spearman([x for x, _ in tr], [y for _, y in tr])
    if sign_rho is None or sign_rho == 0:
        return []
    s = 1.0 if sign_rho > 0 else -1.0
    return [
        (s * float(fv.values[feat]), fv.net_pnl, fv.vol_regime)
        for fv in test if fv.values.get(feat) is not None
    ]


def _categorical_pairs(train: list[FeatureVector], test: list[FeatureVector], feat: str):
    """Target-encoded (predictor, outcome) test pairs for a categorical feature. The
    encoding is each category's TRAIN mean net P&L; unseen categories fall back to the
    train global mean. Encoding fit on train only → no leakage."""
    buckets: dict[str, list[float]] = {}
    for fv in train:
        v = fv.values.get(feat)
        if v is not None:
            buckets.setdefault(str(v), []).append(fv.net_pnl)
    if not buckets:
        return []
    global_mean = sum(fv.net_pnl for fv in train) / len(train)
    enc = {c: sum(ys) / len(ys) for c, ys in buckets.items()}
    return [
        (enc.get(str(fv.values.get(feat)), global_mean), fv.net_pnl, fv.vol_regime)
        for fv in test
    ]


def _oos_pairs_one_direction(
    items: list[FeatureVector], feat: str, *, n_folds: int, embargo_days: int, reverse: bool
):
    """Fit the predictor on each train fold, score its held-out test fold, and pool
    the oriented OOS pairs — for ONE walk direction. Directions are kept separate so
    the forward pass gives the CI and the reverse pass is an independent same-sign
    check (pooling both would double-count rows and narrow the CI artificially)."""
    pair_fn = _numeric_pairs if feat in _NUMERIC else _categorical_pairs
    pooled: list[tuple[float, float, str]] = []
    folds = walk_forward_folds(
        items, lambda fv: fv.entry_date, exit_or_entry,
        n_folds=n_folds, embargo_days=embargo_days, reverse=reverse,
    )
    for fold in folds:
        if len(fold.test) < _MIN_FOLD_TEST:
            continue
        pooled.extend(pair_fn(fold.train, fold.test, feat))
    return pooled


@dataclass(frozen=True)
class FeatureVerdict:
    feature: str
    kind: str  # numeric | categorical
    n_oos: int
    oos_rho: float | None  # forward-direction pooled OOS Spearman (the CI statistic)
    ci_lo: float | None
    ci_hi: float | None
    reverse_rho: float | None  # reverse-direction OOS Spearman (same-sign robustness)
    per_regime: dict[str, float | None]  # regime -> OOS rho (sign check)
    regime_sign_flip: bool
    validated: bool
    note: str


def _regime_signs(pooled: list[tuple[float, float, str]]) -> dict[str, float | None]:
    by_regime: dict[str, list[tuple[float, float]]] = {}
    for p, y, r in pooled:
        by_regime.setdefault(r or "unknown", []).append((p, y))
    out: dict[str, float | None] = {}
    for r, pairs in by_regime.items():
        out[r] = spearman([p for p, _ in pairs], [y for _, y in pairs]) if len(pairs) >= 8 else None
    return out


def validate_feature(
    items: list[FeatureVector], feat: str, *, n_folds: int, embargo_days: int, alpha: float, n_features: int
) -> FeatureVerdict:
    kind = "numeric" if feat in _NUMERIC else "categorical"
    fwd = _oos_pairs_one_direction(items, feat, n_folds=n_folds, embargo_days=embargo_days, reverse=False)
    rev = _oos_pairs_one_direction(items, feat, n_folds=n_folds, embargo_days=embargo_days, reverse=True)
    reverse_rho = spearman([p for p, _, _ in rev], [y for _, y, _ in rev]) if len(rev) >= _MIN_FOLD_TEST else None
    reverse_rho = round(reverse_rho, 4) if reverse_rho is not None else None
    if len(fwd) < _MIN_POOLED:
        return FeatureVerdict(
            feature=feat, kind=kind, n_oos=len(fwd), oos_rho=None, ci_lo=None, ci_hi=None,
            reverse_rho=reverse_rho, per_regime={}, regime_sign_flip=False, validated=False,
            note=f"underpowered: {len(fwd)} forward OOS pairs (< {_MIN_POOLED}).",
        )
    xs = [p for p, _, _ in fwd]
    ys = [y for _, y, _ in fwd]
    corr_alpha = bonferroni_alpha(alpha, n_features)  # family-wise correction
    ci = bootstrap_corr_ci(xs, ys, alpha=corr_alpha)
    per_regime = _regime_signs(fwd)
    signs = [1 if v > 0 else -1 for v in per_regime.values() if v is not None and abs(v) > 1e-9]
    flip = len(set(signs)) > 1
    if ci is None:
        return FeatureVerdict(
            feature=feat, kind=kind, n_oos=len(fwd), oos_rho=None, ci_lo=None, ci_hi=None,
            reverse_rho=reverse_rho, per_regime=per_regime, regime_sign_flip=flip, validated=False,
            note="OOS correlation undefined (degenerate predictor).",
        )
    # Reverse walk must not contradict the forward sign (robustness, not another test).
    reverse_contradicts = reverse_rho is not None and reverse_rho < 0
    validated = ci.lo > 0 and not flip and not reverse_contradicts
    if validated:
        note = (f"validated: forward OOS ρ={ci.point} (Bonferroni CI [{ci.lo}, {ci.hi}] > 0), "
                f"reverse ρ={reverse_rho}, per-regime signs consistent.")
    elif ci.lo > 0 and flip:
        note = f"NOT validated: OOS ρ={ci.point} clears zero but sign FLIPS across regimes (conditional beta)."
    elif ci.lo > 0 and reverse_contradicts:
        note = f"NOT validated: forward ρ={ci.point} but reverse walk contradicts (ρ={reverse_rho})."
    elif ci.hi < 0:
        note = f"NOT validated: OOS ρ={ci.point} is negatively predictive (CI [{ci.lo}, {ci.hi}] < 0)."
    else:
        note = f"NOT validated: OOS ρ={ci.point}, CI [{ci.lo}, {ci.hi}] straddles zero."
    return FeatureVerdict(
        feature=feat, kind=kind, n_oos=len(fwd), oos_rho=ci.point, ci_lo=ci.lo, ci_hi=ci.hi,
        reverse_rho=reverse_rho, per_regime=per_regime, regime_sign_flip=flip,
        validated=validated, note=note,
    )


@dataclass
class FeatureRegistry:
    """The output of a validation run: which features earned weight, and the
    data-derived weights (∝ OOS effect size, normalized to 100). Empty weights are
    the honest, expected default when nothing clears the bar."""

    verdicts: list[FeatureVerdict]
    weights: dict[str, float]
    n_trades: int
    n_features: int
    alpha: float
    corpus_note: str = ""

    @property
    def any_validated(self) -> bool:
        return bool(self.weights)

    def as_dict(self) -> dict:
        return {
            "any_validated": self.any_validated,
            "n_trades": self.n_trades,
            "n_features_tested": self.n_features,
            "alpha": self.alpha,
            "weights": self.weights,
            "corpus_note": self.corpus_note,
            "verdicts": [
                {
                    "feature": v.feature, "kind": v.kind, "n_oos": v.n_oos,
                    "oos_rho": v.oos_rho, "ci_lo": v.ci_lo, "ci_hi": v.ci_hi,
                    "reverse_rho": v.reverse_rho,
                    "per_regime": {r: (round(x, 4) if x is not None else None) for r, x in v.per_regime.items()},
                    "regime_sign_flip": v.regime_sign_flip, "validated": v.validated, "note": v.note,
                }
                for v in self.verdicts
            ],
        }


def build_registry(
    items: list[FeatureVector],
    *,
    features: tuple[str, ...] = ALL_FEATURES,
    n_folds: int = 4,
    embargo_days: int = 7,
    alpha: float = 0.05,
    corpus_note: str = "",
) -> FeatureRegistry:
    """Validate every feature against the corpus and derive weights from what
    survives. Weights are proportional to the validated features' OOS |rho|,
    normalized to sum to 100. Nothing survives → empty weights (honest null)."""
    verdicts = [
        validate_feature(
            items, f, n_folds=n_folds, embargo_days=embargo_days, alpha=alpha,
            n_features=len(features),
        )
        for f in features
    ]
    effect = {v.feature: abs(v.oos_rho) for v in verdicts if v.validated and v.oos_rho is not None}
    total = sum(effect.values())
    weights = {f: round(100 * e / total, 2) for f, e in effect.items()} if total > 0 else {}
    return FeatureRegistry(
        verdicts=verdicts, weights=weights, n_trades=len(items),
        n_features=len(features), alpha=alpha, corpus_note=corpus_note,
    )
