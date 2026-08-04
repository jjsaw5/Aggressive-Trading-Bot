"""Remove signals produced by a mock-provider run from the decision warehouse.

WHY THIS EXISTS: verifying the Phase 1 market-context wiring end-to-end meant
running a real `run_detection` with `PROVIDER_*=mock`. The scan worked — and
warehoused its 84 mock candidates into the live Turso corpus, because
`run_detection` persists unconditionally and `.env` points at Turso. Those rows
are indistinguishable from real signals in every column except one, and they
would enter the capture window as if they were evidence.

The one column that distinguishes them is `market_context.chain_source`, which
Phase 1 added. That is the sole discriminator used here — not a timestamp
window, which would be a guess about what else was running.

SAFETY:
  * `--dry-run` (the default) prints and deletes nothing.
  * Deletes only rows whose `chain_source` is in `--sources` (default: mock).
  * Refuses to run if the matched count differs from `--expect`, when given.
  * Never touches decision_outcomes: a mock row with a graded outcome would mean
    something worse than pollution, so the script stops rather than tidying it.

Usage:
    python scripts/purge_mock_signals.py                      # dry run
    python scripts/purge_mock_signals.py --apply --expect 84
"""

from __future__ import annotations

import argparse

from sqlalchemy import delete, select

from app.db import models as m
from app.db.session import SessionLocal

MOCK_SOURCES = {"mock"}


def _mock_decision_ids(
    session, sources: set[str], model_versions: set[str] | None = None
) -> list[str]:
    """Decision ids warehoused by a run that should not have persisted.

    Two independent discriminators, either of which matches:

    * `chain_source` in `sources` — the original case: a mock-provider run whose
      84 rows reached the live corpus because `run_detection` persists
      unconditionally.
    * `scoring_model_version` in `model_versions` — the second case (2026-08-04):
      a verification run of REAL provider data on an UNMERGED branch, which
      stamped rows with a model version production was not running. The data is
      genuine; its provenance is a developer's working tree, and leaving it makes
      the first rows of a new model's corpus a test artifact.

    Rows predating Phase 1 have no market_context at all; they are left alone.
    Absence of provenance is not evidence of mockery.
    """
    out = []
    for row in session.execute(select(m.DecisionSnapshotRow)).scalars():
        payload = row.payload or {}
        mc = payload.get("market_context") or {}
        if mc.get("chain_source") in sources:
            out.append(row.decision_id)
        elif model_versions and payload.get("scoring_model_version") in model_versions:
            out.append(row.decision_id)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument("--expect", type=int, default=None,
                    help="abort unless exactly this many snapshots match")
    ap.add_argument("--sources", default="mock",
                    help="comma-separated chain_source values to purge")
    ap.add_argument("--model-versions", default="",
                    help="comma-separated scoring_model_version values to purge — for "
                         "rows warehoused by a non-production run of a real provider")
    args = ap.parse_args()
    sources = {s.strip() for s in args.sources.split(",") if s.strip()}
    versions = {s.strip() for s in args.model_versions.split(",") if s.strip()}

    with SessionLocal() as session:
        ids = _mock_decision_ids(session, sources, versions)
        cand_ids = [i.split(":", 1)[1] for i in ids if ":" in i]
        print(f"snapshots matching chain_source {sorted(sources)} "
              f"or model_version {sorted(versions) or '(none)'}: {len(ids)}")

        if not ids:
            print("nothing to do")
            return 0

        graded = session.execute(
            select(m.DecisionOutcomeRow).where(m.DecisionOutcomeRow.decision_id.in_(ids))
        ).scalars().all()
        if graded:
            print(f"ABORT: {len(graded)} of these already have graded outcomes. "
                  "A mock signal with a real grade is a bug to investigate, not "
                  "a row to delete.")
            return 2

        n_cands = len(session.execute(
            select(m.ShortDurationCandidateRow)
            .where(m.ShortDurationCandidateRow.id.in_(cand_ids))
        ).scalars().all())
        n_trans = len(session.execute(
            select(m.CandidateTransitionRow)
            .where(m.CandidateTransitionRow.candidate_id.in_(cand_ids))
        ).scalars().all())
        print(f"  linked short_duration_candidates: {n_cands}")
        print(f"  linked candidate_state_transitions: {n_trans}")

        if args.expect is not None and len(ids) != args.expect:
            print(f"ABORT: expected {args.expect} snapshots, found {len(ids)}.")
            return 2

        if not args.apply:
            print("\nDRY RUN — nothing deleted. Re-run with --apply to delete.")
            return 0

        t = session.execute(
            delete(m.CandidateTransitionRow)
            .where(m.CandidateTransitionRow.candidate_id.in_(cand_ids))
        )
        c = session.execute(
            delete(m.ShortDurationCandidateRow)
            .where(m.ShortDurationCandidateRow.id.in_(cand_ids))
        )
        d = session.execute(
            delete(m.DecisionSnapshotRow).where(m.DecisionSnapshotRow.decision_id.in_(ids))
        )
        session.commit()
        print(f"deleted: transitions={t.rowcount} candidates={c.rowcount} snapshots={d.rowcount}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
