"""The capture window's freeze, made mechanical.

`docs/CAPTURE_WINDOW_PREREGISTRATION.md` §2 freezes `sd-scoring-2026.07-v3` for
the duration of the signal-only capture window:

    No changes to component weights, thresholds, scoring components, or the
    watchlist universe.

    Permitted: data persistence, grading integrity, logging/decomposition, bug
    fixes to non-scoring code.

Phase 1 persists a large amount of newly-recorded market state — NBBO, Greeks,
IV term structure, cost drag, regime. That is squarely permitted, and it is also
exactly the material a future change would be tempted to feed into the score. A
single line doing so would invalidate the window silently: the corpus would span
two different models while every row still reported `sd-scoring-2026.07-v3`.

The freeze is a promise about behaviour, so it is tested rather than documented.
These are not unit tests of a function; they are a lock on an architectural
boundary. If one fails, the question is not "how do I fix the test" but "does
this change end the capture window and require an amendment under §8?".
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[1] / "app"
_SCORING = _APP / "shortduration" / "scoring"
_STRATEGIES = _APP / "shortduration" / "strategies"

# The frozen model. Changing this string is itself a window-ending act.
FROZEN_MODEL_VERSION = "sd-scoring-2026.07-v3"

# Modules Phase 1 introduced. None of them may be reachable from the scorer.
CAPTURE_ONLY_MODULES = {
    # Phase 1 — market state frozen at decision time.
    "app.analytics.market_context",
    "app.domain.market_context",
    # Phase 2 — grading integrity. Every one of these looks at what HAPPENED
    # after a decision, so a scoring component reading any of them would be
    # scoring on the future. That is not merely a freeze violation, it is
    # lookahead: the resulting corpus would be unusable rather than just
    # mislabeled.
    "app.analytics.intraday_settlement",
    "app.analytics.policy_settlement",
    "app.analytics.cost_stress",
    "app.analytics.slippage",
}


def _module_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _imported_modules(path: Path) -> set[str]:
    """Every module name this file imports, absolute form, at any nesting depth.

    Walks the AST rather than grepping so an import inside a function body — the
    lazy-import style this codebase uses throughout — is caught too.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


@pytest.mark.parametrize("path", _module_files(_SCORING), ids=lambda p: p.name)
def test_no_scoring_module_can_see_the_captured_market_context(path: Path) -> None:
    """THE Phase 1 invariant.

    Persisting NBBO/Greeks/regime is permitted precisely because it does not
    change what the scorer computes. The moment a scoring component imports that
    data, the frozen model is no longer frozen and the capture window is void.
    """
    leaked = _imported_modules(path) & CAPTURE_ONLY_MODULES
    assert not leaked, (
        f"{path.name} imports {sorted(leaked)}. Phase 1 data is recorded, never "
        "scored — see CAPTURE_WINDOW_PREREGISTRATION.md §2. If this is "
        "deliberate, the capture window has ended and §8 requires a dated "
        "amendment before the import lands."
    )


@pytest.mark.parametrize("path", _module_files(_STRATEGIES), ids=lambda p: p.name)
def test_no_strategy_module_can_see_the_captured_market_context(path: Path) -> None:
    """Detection is upstream of scoring; a setup gated on the new data would
    change which candidates exist, which changes the corpus just as surely as
    changing a weight would."""
    leaked = _imported_modules(path) & CAPTURE_ONLY_MODULES
    assert not leaked, f"{path.name} imports {sorted(leaked)} — see §2 of the pre-registration."


def test_the_frozen_scoring_version_is_still_the_configured_one() -> None:
    """A silent version bump would split the corpus mid-window while every row
    still claimed one lineage."""
    from app.config import settings

    assert settings.scoring_model_version == FROZEN_MODEL_VERSION, (
        "The scoring model version changed during the capture window. Per §2 "
        "the model is frozen; per §8 any deviation needs a dated amendment."
    )


def test_the_market_context_never_reaches_a_score_field() -> None:
    """End-to-end: a candidate carrying a full market context scores identically
    to one carrying none.

    The import guards above catch the static path. This catches the case where
    the data arrives through an object the scorer already holds.
    """
    from app.domain.market_context import LegQuote, MarketContext
    from app.domain.shortduration import ScoreCard

    # A scorecard is the scorer's entire output surface. If market context ever
    # entered scoring it would have to show up here.
    card = ScoreCard(
        dte_category=__import__("app.domain.enums", fromlist=["x"]).DTECategory.SHORT_DTE,
        total=71.0, overall_confidence=0.71,
        weights={"daily_trend": 100.0},
    )
    context_fields = set(MarketContext.model_fields) | set(LegQuote.model_fields)
    scorecard_fields = set(ScoreCard.model_fields)
    # `dte_category` is legitimately shared vocabulary, nothing else may be.
    overlap = (context_fields & scorecard_fields) - {"dte_category"}
    assert not overlap, (
        f"ScoreCard and MarketContext share {sorted(overlap)}. Captured market "
        "state must not be part of the scoring surface."
    )
    assert card.total == 71.0
