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

from app.db.models import CandidateRow, PaperTradeRow, ProposalRow, ScanRow
from app.db.session import SessionLocal
from app.domain.candidates import TradeCandidate
from app.domain.trades import OrderProposal, PaperTrade


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
