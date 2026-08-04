"""The guarded path set must mean the same thing everywhere it is written down.

It is written three times, in three notations:

  1. `GUARDED_RE`    — a regex, in `.github/workflows/ci.yml` (the blocking check)
  2. `GUARDED_PATHS` — a space-separated list, same file (the informational diff)
  3. `docs/FREEZE_POINT.md` — a `git diff` the reader is told to run by hand

They drifted. The path guard was widened on 2026-08-03 to cover contract
selection, while the informational diff still looked at two directories — so CI
printed "Empty — scoring paths unchanged since the freeze" about paths it was not
looking at, and did so on the very PR that changed them. The tag name was
hardcoded in the workflow at the same time and went stale the moment the model
moved to v4.0, pointing the comparison at a superseded baseline.

Reassurance from a check that is not looking is worse than no check. These tests
make the three copies fail loudly instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
FREEZE_DOC = ROOT / "docs" / "FREEZE_POINT.md"


def _ci() -> str:
    return CI.read_text(encoding="utf-8")


def _doc() -> str:
    return FREEZE_DOC.read_text(encoding="utf-8")


def _guarded_re_members() -> set[str]:
    """The alternation inside GUARDED_RE, unescaped back to plain paths."""
    m = re.search(r"GUARDED_RE:\s*'(\^\((.*?)\))'", _ci())
    assert m, "GUARDED_RE not found in ci.yml"
    return {alt.replace("\\.", ".") for alt in m.group(2).split("|")}


def _guarded_paths_members() -> set[str]:
    m = re.search(r"GUARDED_PATHS:\s*'(.*?)'", _ci())
    assert m, "GUARDED_PATHS not found in ci.yml"
    return set(m.group(1).split())


def test_both_files_exist() -> None:
    # A moved file must fail loudly rather than vacuously passing every
    # assertion below against an empty string.
    assert CI.is_file() and FREEZE_DOC.is_file()


def test_the_regex_and_the_path_list_describe_the_same_set() -> None:
    """THE drift that happened. One check blocked; the other looked elsewhere."""
    assert _guarded_re_members() == _guarded_paths_members()


def test_every_guarded_path_actually_exists() -> None:
    """A guarded path that has been renamed silently stops guarding anything."""
    missing = [p for p in _guarded_paths_members() if not (ROOT / p).exists()]
    assert not missing, f"guarded paths that no longer exist: {missing}"


def test_the_documented_check_covers_the_whole_guarded_set() -> None:
    """`docs/FREEZE_POINT.md` tells a human which diff to run. If it lists fewer
    paths than CI guards, a hand check gives false comfort."""
    doc = _doc()
    missing = [p for p in _guarded_paths_members() if p not in doc]
    assert not missing, (
        f"docs/FREEZE_POINT.md 'The check' omits guarded paths: {missing}"
    )


def test_the_workflow_no_longer_hardcodes_the_freeze_tag() -> None:
    """It did, and the tag went stale the moment the model moved."""
    ci = _ci()
    hardcoded = re.findall(r'REF="freeze/[^"$]+"', ci)
    assert not hardcoded, f"freeze tag hardcoded in ci.yml: {hardcoded}"
    assert "grep -oE 'freeze/" in ci, "ci.yml must read the tag name from the doc"


# --- The doc header is machine-read; its shape is load-bearing ----------------
def test_the_first_sha_in_the_doc_is_the_current_freeze_point() -> None:
    """CI takes the FIRST 40-hex string. The superseded-freeze-points table also
    contains SHAs, so ordering in this document is a correctness property."""
    shas = re.findall(r"\b[0-9a-f]{40}\b", _doc())
    assert shas, "no 40-hex SHA in docs/FREEZE_POINT.md"
    header = _doc().split("## Superseded", 1)[0]
    assert shas[0] in header, (
        "the first SHA in the document is not in the header — CI would resolve "
        "the freeze point to a superseded entry"
    )


def test_the_first_tag_in_the_doc_is_the_current_freeze_point() -> None:
    tags = re.findall(r"freeze/[A-Za-z0-9._-]+", _doc())
    assert tags, "no freeze/ tag in docs/FREEZE_POINT.md"
    header = _doc().split("## Superseded", 1)[0]
    assert tags[0] in header


def test_the_doc_header_names_the_configured_model_version() -> None:
    """The freeze point and the shipped model must not disagree."""
    from app.config import settings

    header = _doc().split("## Superseded", 1)[0]
    assert settings.scoring_model_version in header, (
        f"docs/FREEZE_POINT.md header does not name {settings.scoring_model_version}"
    )


@pytest.mark.parametrize("path", [
    "app/shortduration/scoring/",
    "app/providers/unusual_whales/client.py",
    "app/engine/contract_selection.py",
    "app/shortduration/contracts.py",
])
def test_the_paths_each_finding_added_are_still_guarded(path: str) -> None:
    """Named individually so removing one is a deliberate, visible act.

    scoring/ is the original freeze. The provider file was added by FINDING_01.
    The two selection paths were added by Amendment 2.
    """
    assert path in _guarded_paths_members()
