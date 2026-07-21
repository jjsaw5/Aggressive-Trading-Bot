"""Phase 7 — migration validation: the full chain applies, is add-only, and the
0004 promotion is reversible + idempotent (inspector-guarded)."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_TABLE = "short_duration_candidates"
_NEW_COLS = {"scoring_model_version", "risk_policy_version"}


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    env.pop("TURSO_DATABASE_URL", None)  # force the sqlite url via database_url
    env.pop("TURSO_AUTH_TOKEN", None)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_REPO, env=env, capture_output=True, text=True, timeout=120,
    )


def _columns(db_path: Path, table: str) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


@pytest.mark.skipif(shutil.which("alembic") is None and not (_REPO / "alembic.ini").exists(),
                    reason="alembic not available")
def test_full_chain_up_down_is_reversible_and_idempotent(tmp_path) -> None:
    db = tmp_path / "mig.db"

    up = _alembic(db, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    cols = _columns(db, _TABLE)
    assert _NEW_COLS <= cols, f"0004 columns missing after upgrade: {cols}"

    down = _alembic(db, "downgrade", "-1")
    assert down.returncode == 0, down.stderr
    assert not (_NEW_COLS & _columns(db, _TABLE)), "0004 downgrade left its columns behind"

    # Re-applying is clean (add-only + inspector-guarded).
    up2 = _alembic(db, "upgrade", "head")
    assert up2.returncode == 0, up2.stderr
    assert _NEW_COLS <= _columns(db, _TABLE)


@pytest.mark.skipif(not (_REPO / "alembic.ini").exists(), reason="alembic not configured")
def test_single_head() -> None:
    heads = _alembic(Path("/tmp/_unused.db"), "heads")
    # Exactly one head revision — a linear, unambiguous chain.
    assert heads.returncode == 0, heads.stderr
    assert heads.stdout.count("(head)") == 1, heads.stdout
