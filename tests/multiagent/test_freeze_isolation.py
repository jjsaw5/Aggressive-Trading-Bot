"""The multi-agent subsystem must not touch the frozen short-duration model.

`CLAUDE.md` §2 freezes `sd-scoring-2026.08-v4.1` for the signal-only capture
window, and FINDING_01 established that the freeze is about **behaviour, not
about which files you edited** — a scoring input sat unread by any live provider
for the whole life of v3, so populating it from outside `app/shortduration/`
changed the shipped model with no diff under `scoring/`.

This subsystem is additive, and these tests make that a mechanical fact rather
than an intention:

* it does not import the frozen scorer, its strategies, or its contract selection;
* the frozen scorer does not import it;
* it does not touch any freeze-guarded path;
* it does not change what any provider populates;
* its persistence is new tables only.

If one of these fails, the question is not "how do I make this pass". It is
"does this change end the capture window?"
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MULTIAGENT = REPO / "app" / "multiagent"
CI = REPO / ".github" / "workflows" / "ci.yml"

# Modules the frozen model is built from. Nothing under app/multiagent/ may
# import any of them — not to reuse a helper, not to compare a score.
FROZEN_PACKAGES = (
    "app.shortduration.scoring",
    "app.shortduration.strategies",
    "app.shortduration.contracts",
    "app.engine.contract_selection",
    "app.engine.iv_context",
)


def _python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _guarded_regex() -> str:
    match = re.search(r"GUARDED_RE:\s*'([^']+)'", CI.read_text(encoding="utf-8"))
    assert match, "GUARDED_RE not found in ci.yml — the freeze guard has moved"
    return match.group(1)


def test_the_multiagent_package_does_not_import_the_frozen_model():
    offenders: list[str] = []
    for path in _python_files(MULTIAGENT):
        for name in _imports(path):
            if any(name == pkg or name.startswith(pkg + ".") for pkg in FROZEN_PACKAGES):
                offenders.append(f"{path.relative_to(REPO)} imports {name}")
    assert not offenders, (
        "the multi-agent subsystem must not reach into the frozen scoring model:\n"
        + "\n".join(offenders)
    )


def test_the_frozen_model_does_not_import_the_multiagent_package():
    """The dependency must not run the other way either.

    If the frozen scorer imported anything from here, a change in this
    subsystem could alter what it computes — which is precisely FINDING_01.
    """
    offenders: list[str] = []
    for root in ("app/shortduration", "app/engine"):
        for path in _python_files(REPO / root):
            for name in _imports(path):
                if name.startswith("app.multiagent"):
                    offenders.append(f"{path.relative_to(REPO)} imports {name}")
    assert not offenders, "\n".join(offenders)


def test_the_multiagent_package_touches_no_freeze_guarded_path():
    """Every file this subsystem owns sits outside the guarded set."""
    guarded = re.compile(_guarded_regex())
    offenders = [
        str(p.relative_to(REPO))
        for p in _python_files(MULTIAGENT)
        if guarded.match(str(p.relative_to(REPO)))
    ]
    assert not offenders, "\n".join(offenders)


def test_the_new_research_mock_lives_outside_the_guarded_provider():
    """The richer news corpus is a new module, not an edit to the guarded mock.

    `app/providers/mock/provider.py` is freeze-guarded. Adding varied news to it
    would have been the obvious move and would have implicated the capture
    window for a subsystem with nothing to do with it.
    """
    guarded = re.compile(_guarded_regex())
    research_mock = "app/multiagent/providers/mock_research.py"
    assert (REPO / research_mock).exists()
    assert not guarded.match(research_mock)
    assert guarded.match("app/providers/mock/provider.py"), (
        "the platform mock is expected to be guarded; if it is not, this test's premise changed"
    )


def test_the_frozen_scoring_version_is_untouched():
    """Bumping it is a window-ending act and cannot happen as a side effect."""
    from app.config import settings
    from tests.test_scoring_freeze import FROZEN_MODEL_VERSION

    assert settings.scoring_model_version == FROZEN_MODEL_VERSION


def test_no_guarded_file_is_modified_in_the_working_tree():
    """Belt and braces: the git diff itself contains no guarded path."""
    guarded = re.compile(_guarded_regex())
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - git absent
        pytest.skip("git is unavailable")
    if proc.returncode != 0:  # pragma: no cover
        pytest.skip("git status failed")

    touched = [
        line[3:].strip()
        for line in proc.stdout.splitlines()
        if line[3:].strip() and guarded.match(line[3:].strip())
    ]
    assert not touched, (
        "a freeze-guarded path has uncommitted changes:\n" + "\n".join(touched)
    )


def test_the_multiagent_schema_is_additive_only():
    """Every table this subsystem owns is new and prefixed."""
    from app.multiagent.db.models import ALL_TABLES

    for table in ALL_TABLES:
        assert table.__tablename__.startswith("ma_"), (
            f"{table.__tablename__} is not namespaced; a collision with an existing table "
            "could change what the frozen model reads"
        )


def test_the_migration_creates_tables_and_alters_none():
    """An ALTER on an existing table would be a behaviour change in disguise."""
    migration = REPO / "alembic" / "versions" / "0007_multiagent_research.py"
    source = migration.read_text(encoding="utf-8")
    upgrade = source.split("def upgrade() -> None:", 1)[1].split("def downgrade", 1)[0]

    assert "op.alter_column" not in upgrade
    assert "op.drop_column" not in upgrade
    assert "op.drop_table" not in upgrade
    # And every created table is namespaced.
    for name in re.findall(r"op\.create_table\('([^']+)'", upgrade):
        assert name.startswith("ma_"), f"migration creates non-namespaced table {name}"


def test_the_subsystem_does_not_change_which_fields_providers_populate():
    """FINDING_01's actual mechanism: populating a previously-unread field.

    This subsystem only READS providers. It defines one new provider
    (`ResearchMockProvider`) implementing existing capabilities, and adds no
    field to any domain model the frozen scorer reads.
    """
    from app.domain.options import IVContext
    from tests.test_provider_scoring_contract import SCORED_IV_FIELDS

    # The fields the frozen scorer reads still exist and are unchanged in shape.
    for field in SCORED_IV_FIELDS:
        assert field in IVContext.model_fields

    # And nothing under app/multiagent/ writes to an IVContext.
    offenders = [
        str(p.relative_to(REPO))
        for p in _python_files(MULTIAGENT)
        if re.search(r"\bIVContext\s*\(", p.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "the multi-agent subsystem constructs an IVContext, which could change what the "
        "frozen scorer sees:\n" + "\n".join(offenders)
    )


def test_the_multiagent_scoring_version_is_distinct_from_the_frozen_one(methodology):
    """Two models, two version strings, never confusable in a stored row."""
    from app.config import settings

    assert methodology.version != settings.scoring_model_version
    assert methodology.version.startswith("ma-")


def test_a_persisted_run_records_both_versions():
    """A stored recommendation names its own methodology AND the platform's model."""
    from app.multiagent.db.models import MARunRow

    assert "methodology_version" in MARunRow.__table__.columns
    assert "scoring_model_version" in MARunRow.__table__.columns
