"""Synchronous persistence repository (durable backend: Turso / libSQL).

The single place that translates domain objects (Pydantic) to/from ORM rows.
Rich domain objects are stored as a JSON `payload` for faithful replay, while
ranking/filter fields are promoted to indexed columns. Callers depend on these
functions, never on the ORM directly.

Each function is self-managing: it opens its own session and commits, so async
callers can hand the whole call to a threadpool without juggling a session.
"""

from __future__ import annotations

from sqlalchemy import delete, select

from app.db.models import (
    CandidateRow,
    DecisionOutcomeRow,
    DecisionSnapshotRow,
    PaperTradeRow,
    ProposalRow,
    ScanRow,
    TierMemberRow,
)
from app.db.session import SessionLocal
from app.domain.candidates import TradeCandidate
from app.domain.outcomes import DecisionOutcome, DecisionSnapshot
from app.domain.trades import OrderProposal, PaperTrade
from app.tiers.models import TierMember


# --- Scans & candidates ------------------------------------------------------
def save_scan(
    scan_id: str,
    universe: list[str],
    candidates: list[TradeCandidate],
) -> None:
    actionable = sum(c.is_actionable for c in candidates)
    with SessionLocal() as session:
        session.merge(
            ScanRow(
                scan_id=scan_id,
                universe=universe,
                candidate_count=len(candidates),
                actionable_count=actionable,
            )
        )
        # Fresh scan_id per run; clear any prior rows defensively before insert.
        session.execute(delete(CandidateRow).where(CandidateRow.scan_id == scan_id))
        for c in candidates:
            session.add(
                CandidateRow(
                    scan_id=c.scan_id,
                    symbol=c.symbol,
                    status=c.status.value,
                    direction=c.direction.value,
                    composite_score=c.composite_score,
                    payload=c.model_dump(mode="json"),
                )
            )
        session.commit()


def list_scans(limit: int = 20) -> list[ScanRow]:
    with SessionLocal() as session:
        res = session.execute(
            select(ScanRow).order_by(ScanRow.created_at.desc()).limit(limit)
        )
        return list(res.scalars().all())


def get_scan_candidates(scan_id: str) -> list[TradeCandidate]:
    with SessionLocal() as session:
        res = session.execute(
            select(CandidateRow)
            .where(CandidateRow.scan_id == scan_id)
            .order_by(CandidateRow.composite_score.desc())
        )
        return [TradeCandidate.model_validate(r.payload) for r in res.scalars().all()]


def get_candidate(scan_id: str, symbol: str) -> TradeCandidate | None:
    with SessionLocal() as session:
        res = session.execute(
            select(CandidateRow).where(
                CandidateRow.scan_id == scan_id,
                CandidateRow.symbol == symbol.upper(),
            )
        )
        row = res.scalars().first()
        return TradeCandidate.model_validate(row.payload) if row else None


# --- Proposals ---------------------------------------------------------------
def save_proposal(proposal: OrderProposal) -> None:
    with SessionLocal() as session:
        session.merge(
            ProposalRow(
                id=proposal.id,
                scan_id=proposal.scan_id,
                symbol=proposal.symbol,
                status=proposal.status.value,
                max_loss_usd=proposal.trade_plan.risk.max_loss_usd,
                approved_by=proposal.approved_by,
                payload=proposal.model_dump(mode="json"),
            )
        )
        session.commit()


def get_proposal(proposal_id: str) -> OrderProposal | None:
    with SessionLocal() as session:
        row = session.get(ProposalRow, proposal_id)
        return OrderProposal.model_validate(row.payload) if row else None


def list_proposals(limit: int = 50) -> list[OrderProposal]:
    with SessionLocal() as session:
        res = session.execute(
            select(ProposalRow).order_by(ProposalRow.created_at.desc()).limit(limit)
        )
        return [OrderProposal.model_validate(r.payload) for r in res.scalars().all()]


# --- Paper trades ------------------------------------------------------------
def save_paper_trade(trade: PaperTrade) -> None:
    with SessionLocal() as session:
        session.merge(
            PaperTradeRow(
                id=trade.id,
                scan_id=trade.scan_id,
                symbol=trade.symbol,
                status=trade.status.value,
                opened_at=trade.opened_at,
                closed_at=trade.closed_at,
                realized_pnl_usd=trade.realized_pnl_usd,
                mfe_usd=trade.mfe_usd,
                mae_usd=trade.mae_usd,
                payload=trade.model_dump(mode="json"),
            )
        )
        session.commit()


def get_paper_trade(trade_id: str) -> PaperTrade | None:
    with SessionLocal() as session:
        row = session.get(PaperTradeRow, trade_id)
        return PaperTrade.model_validate(row.payload) if row else None


def list_paper_trades(limit: int = 50) -> list[PaperTrade]:
    with SessionLocal() as session:
        res = session.execute(
            select(PaperTradeRow).order_by(PaperTradeRow.created_at.desc()).limit(limit)
        )
        return [PaperTrade.model_validate(r.payload) for r in res.scalars().all()]


# --- Decision snapshots (the learning warehouse) -----------------------------
def _snapshot_row(s: DecisionSnapshot) -> DecisionSnapshotRow:
    return DecisionSnapshotRow(
        decision_id=s.decision_id,
        scan_id=s.scan_id,
        symbol=s.symbol,
        source=s.source.value,
        direction=s.direction.value,
        strategy=s.strategy.value,
        generated_at=s.generated_at,
        composite_score=s.composite_score,
        probability_of_profit=s.probability_of_profit,
        entry_spot=s.entry_spot,
        iv_rank=s.iv_rank,
        max_loss_usd=s.max_loss_usd,
        expiration=s.expiration,
        payload=s.model_dump(mode="json"),
    )


def save_snapshots(snapshots: list[DecisionSnapshot]) -> int:
    """Warehouse decision snapshots. Idempotent: an existing decision_id is left
    untouched so a re-run never resets its resolution status. Returns new count."""
    added = 0
    with SessionLocal() as session:
        for s in snapshots:
            if session.get(DecisionSnapshotRow, s.decision_id) is not None:
                continue
            session.add(_snapshot_row(s))
            added += 1
        session.commit()
    return added


def list_snapshots(limit: int = 100, status: str | None = None) -> list[DecisionSnapshot]:
    with SessionLocal() as session:
        stmt = select(DecisionSnapshotRow)
        if status is not None:
            stmt = stmt.where(DecisionSnapshotRow.resolution_status == status)
        stmt = stmt.order_by(DecisionSnapshotRow.generated_at.desc()).limit(limit)
        res = session.execute(stmt)
        return [DecisionSnapshot.model_validate(r.payload) for r in res.scalars().all()]


def get_snapshot(decision_id: str) -> DecisionSnapshot | None:
    with SessionLocal() as session:
        row = session.get(DecisionSnapshotRow, decision_id)
        return DecisionSnapshot.model_validate(row.payload) if row else None


def save_outcome(outcome: DecisionOutcome) -> None:
    """Record a realized outcome and promote its snapshot to 'resolved'."""
    with SessionLocal() as session:
        session.add(
            DecisionOutcomeRow(
                decision_id=outcome.decision_id,
                symbol=outcome.symbol,
                horizon_label=outcome.horizon_label,
                resolved_at=outcome.resolved_at,
                result=outcome.result.value,
                direction_correct=outcome.direction_correct,
                underlying_return_pct=outcome.underlying_return_pct,
                realized_pnl_usd=outcome.realized_pnl_usd,
                outcome_source=outcome.outcome_source,
                payload=outcome.model_dump(mode="json"),
            )
        )
        snap = session.get(DecisionSnapshotRow, outcome.decision_id)
        if snap is not None:
            snap.resolution_status = "resolved"
        session.commit()


def list_outcomes(limit: int = 200) -> list[DecisionOutcome]:
    with SessionLocal() as session:
        res = session.execute(
            select(DecisionOutcomeRow)
            .order_by(DecisionOutcomeRow.resolved_at.desc())
            .limit(limit)
        )
        return [DecisionOutcome.model_validate(r.payload) for r in res.scalars().all()]


def get_outcomes_for(decision_id: str) -> list[DecisionOutcome]:
    with SessionLocal() as session:
        res = session.execute(
            select(DecisionOutcomeRow).where(
                DecisionOutcomeRow.decision_id == decision_id
            )
        )
        return [DecisionOutcome.model_validate(r.payload) for r in res.scalars().all()]


# --- Tier membership (funnel state) ------------------------------------------
def replace_tier(tier: int, members: list[TierMember]) -> None:
    """Overwrite the membership of one tier atomically (promotion + demotion)."""
    with SessionLocal() as session:
        session.execute(delete(TierMemberRow).where(TierMemberRow.tier == tier))
        for m in members:
            session.add(
                TierMemberRow(
                    tier=int(m.tier),
                    symbol=m.symbol.upper(),
                    score=m.score,
                    reason=m.reason[:128],
                    payload=m.model_dump(mode="json"),
                )
            )
        session.commit()


def list_tier(tier: int) -> list[TierMember]:
    with SessionLocal() as session:
        res = session.execute(
            select(TierMemberRow)
            .where(TierMemberRow.tier == tier)
            .order_by(TierMemberRow.score.desc())
        )
        return [TierMember.model_validate(r.payload) for r in res.scalars().all()]


def list_all_tiers() -> list[TierMember]:
    with SessionLocal() as session:
        res = session.execute(
            select(TierMemberRow).order_by(
                TierMemberRow.tier.desc(), TierMemberRow.score.desc()
            )
        )
        return [TierMember.model_validate(r.payload) for r in res.scalars().all()]


def fetch_calibration_data(
    limit: int = 1000,
) -> tuple[list[DecisionSnapshot], list[DecisionOutcome]]:
    """All snapshots + outcomes for the scorecard, in one place."""
    with SessionLocal() as session:
        srows = session.execute(
            select(DecisionSnapshotRow)
            .order_by(DecisionSnapshotRow.generated_at.desc())
            .limit(limit)
        )
        snaps = [DecisionSnapshot.model_validate(r.payload) for r in srows.scalars().all()]
        orows = session.execute(
            select(DecisionOutcomeRow)
            .order_by(DecisionOutcomeRow.resolved_at.desc())
            .limit(limit)
        )
        outs = [DecisionOutcome.model_validate(r.payload) for r in orows.scalars().all()]
        return snaps, outs
