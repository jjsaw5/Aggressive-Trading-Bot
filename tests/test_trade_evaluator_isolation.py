"""The evaluator must never write to the capture corpus.

This is a control, not a unit test. Unconditional persistence has already
polluted `decision_snapshots` TWICE in this repository — both times from code
that wrote as a side effect of merely being called, and both times discovered by
inspection afterwards rather than by any check. The second incident stamped rows
with a `scoring_model_version` that had not shipped, from an unmerged tree.

An evaluator is exactly the shape that causes a third: it is called ad hoc, on
arbitrary tickers, repeatedly, and it produces something that looks enough like a
decision to be tempting to warehouse. It must not be. A user can evaluate the
same bad idea forty times; counting those as decisions would move the base rate
the conviction gate is measured against, and the gate is the thing standing
between UNCALIBRATED and a claim.

So: the engine writes nothing at all, and the API layer writes only to
`trade_evaluations`.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from app.domain.evaluation import StructureType
from app.engine import trade_evaluator
from app.research import evaluate as evaluate_mod

_ROOT = Path(__file__).resolve().parents[1]

# Repository functions that reach the capture corpus or the signal tables. None
# of these may be referenced from the evaluator's own modules.
_FORBIDDEN_CALLS = {
    "save_snapshots",
    "save_outcome",
    "save_short_duration_candidate",
    "save_short_duration_trade",
    "save_candidate_transition",
    "save_scan",
    "save_proposal",
    "save_paper_trade",
    "run_detection",
}

# The tables the evaluator is allowed to write. Exactly one.
_ALLOWED_TABLES = {"trade_evaluations"}

_EVALUATOR_MODULES = [trade_evaluator, evaluate_mod]


def _names_used(module) -> set[str]:
    """Every attribute and function name mentioned anywhere in a module's source."""
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.split(".")[-1])
            if node.asname:
                names.add(node.asname)
    return names


@pytest.mark.parametrize("module", _EVALUATOR_MODULES, ids=lambda m: m.__name__)
def test_the_evaluator_never_names_a_persistence_function(module) -> None:
    """Catches the import before it can become a call."""
    leaked = _names_used(module) & _FORBIDDEN_CALLS
    assert not leaked, (
        f"{module.__name__} references {sorted(leaked)} — the evaluator must not "
        "write to the capture corpus, and must not trigger detection, which "
        "persists as a side effect."
    )


@pytest.mark.parametrize("module", _EVALUATOR_MODULES, ids=lambda m: m.__name__)
def test_the_evaluator_does_not_import_the_repository_at_all(module) -> None:
    """Defence in depth: no repository import means no accidental future call.

    Persistence belongs in the API layer, where it is visible next to the flag
    that governs it.
    """
    src = inspect.getsource(module)
    assert "from app.db" not in src and "import app.db" not in src, (
        f"{module.__name__} imports the persistence layer; keep writes in the route."
    )


def test_run_detection_is_not_reachable_from_the_evaluation_path() -> None:
    """`app.research.symbol` DOES call `run_detection` for its suggested plays,
    and `run_detection` persists unconditionally. The evaluation path deliberately
    does not reuse that fan-out, and this pins the separation — the two modules
    look similar enough that merging them would be an easy, silent mistake.
    """
    symbol_src = (_ROOT / "app" / "research" / "symbol.py").read_text()
    assert "run_detection" in _names_used_in_source(symbol_src), (
        "premise changed: symbol.py no longer calls run_detection, so this test "
        "is guarding a boundary that has moved — re-derive it."
    )
    # AST, not raw text: this module's own docstring discusses `run_detection`
    # by name, and a substring search would flag the explanation of the rule as
    # a violation of it.
    eval_names = _names_used_in_source((_ROOT / "app" / "research" / "evaluate.py").read_text())
    assert "run_detection" not in eval_names
    assert "build_symbol_report" not in eval_names


def test_the_route_writes_only_to_the_evaluations_table() -> None:
    src = (_ROOT / "app" / "api" / "routes" / "research.py").read_text()
    assert "save_trade_evaluation" in src
    assert not (_FORBIDDEN_CALLS & set(_names_used_in_source(src)))


def _names_used_in_source(src: str) -> set[str]:
    tree = ast.parse(src)
    return {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    }


def test_the_repository_writer_targets_only_the_evaluations_table() -> None:
    """The save function must not be a general-purpose writer."""
    from app.db import repository

    src = textwrap.dedent(inspect.getsource(repository.save_trade_evaluation))
    tree = ast.parse(src)
    rows = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id.endswith("Row")
    }
    assert rows == {"TradeEvaluationRow"}, f"also writes {sorted(rows)}"


def test_the_evaluations_table_is_not_the_snapshots_table() -> None:
    from app.db.models import DecisionSnapshotRow, TradeEvaluationRow

    assert TradeEvaluationRow.__tablename__ in _ALLOWED_TABLES
    assert TradeEvaluationRow.__tablename__ != DecisionSnapshotRow.__tablename__


def test_calibration_does_not_read_the_evaluations_table() -> None:
    """Evaluations are graded opinions about hypotheticals. If they ever reached
    the scorecard they would contaminate the corpus the conviction gate reads."""
    src = (_ROOT / "app" / "analytics" / "calibration.py").read_text()
    assert "trade_evaluation" not in src.lower()


def test_scoring_a_trade_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The behavioural check behind the static ones: run a real evaluation with
    every repository writer booby-trapped, and confirm none of them fires."""
    from app.db import repository

    fired: list[str] = []
    for name in dir(repository):
        if name.startswith("save_") or name.startswith("upsert_"):
            monkeypatch.setattr(
                repository, name,
                (lambda n: lambda *a, **k: fired.append(n))(name),
                raising=False,
            )

    from tests.test_trade_evaluator import _inputs

    trade_evaluator.evaluate(
        symbol="AAA", structure=StructureType.CALL_DEBIT_SPREAD,
        horizon="2026-09-18", inputs=_inputs(), long_strike=100.0, short_strike=105.0,
    )
    assert fired == [], f"evaluation triggered persistence: {fired}"
