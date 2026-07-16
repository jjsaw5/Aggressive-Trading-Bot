"""Async persistence repository.

The single place that translates domain objects (Pydantic) to/from ORM rows.
Rich domain objects are stored as a JSON `payload` for faithful replay, while
ranking/filter fields are promoted to indexed columns. Callers depend on these
functions, never on the ORM directly.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CandidateRow, PaperTradeRow, ProposalRow, ScanRow
from app.domain.candidates import TradeCandidate
from app.domain.trades import OrderProposal, PaperTrade


# --- Scans & candidates ------------------------------------------------------
async def save_scan(
    session: AsyncSession,
    scan_id: str,
    universe: list[str],
    candidates: list[TradeCandidate],
) -> None:
    actionable = sum(c.is_actionable for c in candidates)
    await session.merge(
        ScanRow(
            scan_id=scan_id,
            universe=universe,
            candidate_count=len(candidates),
            actionable_count=actionable,
        )
    )
    # Fresh scan_id per run; clear any prior rows defensively before insert.
    await session.execute(delete(CandidateRow).where(CandidateRow.scan_id == scan_id))
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
    await session.commit()


async def list_scans(session: AsyncSession, limit: int = 20) -> list[ScanRow]:
    res = await session.execute(
        select(ScanRow).order_by(ScanRow.created_at.desc()).limit(limit)
    )
    return list(res.scalars().all())


async def get_scan_candidates(
    session: AsyncSession, scan_id: str
) -> list[TradeCandidate]:
    res = await session.execute(
        select(CandidateRow)
        .where(CandidateRow.scan_id == scan_id)
        .order_by(CandidateRow.composite_score.desc())
    )
    return [TradeCandidate.model_validate(r.payload) for r in res.scalars().all()]


async def get_candidate(
    session: AsyncSession, scan_id: str, symbol: str
) -> TradeCandidate | None:
    res = await session.execute(
        select(CandidateRow).where(
            CandidateRow.scan_id == scan_id,
            CandidateRow.symbol == symbol.upper(),
        )
    )
    row = res.scalars().first()
    return TradeCandidate.model_validate(row.payload) if row else None


# --- Proposals ---------------------------------------------------------------
async def save_proposal(session: AsyncSession, proposal: OrderProposal) -> None:
    await session.merge(
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
    await session.commit()


async def get_proposal(session: AsyncSession, proposal_id: str) -> OrderProposal | None:
    row = await session.get(ProposalRow, proposal_id)
    return OrderProposal.model_validate(row.payload) if row else None


async def list_proposals(session: AsyncSession, limit: int = 50) -> list[OrderProposal]:
    res = await session.execute(
        select(ProposalRow).order_by(ProposalRow.created_at.desc()).limit(limit)
    )
    return [OrderProposal.model_validate(r.payload) for r in res.scalars().all()]


# --- Paper trades ------------------------------------------------------------
async def save_paper_trade(session: AsyncSession, trade: PaperTrade) -> None:
    await session.merge(
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
    await session.commit()


async def get_paper_trade(session: AsyncSession, trade_id: str) -> PaperTrade | None:
    row = await session.get(PaperTradeRow, trade_id)
    return PaperTrade.model_validate(row.payload) if row else None


async def list_paper_trades(session: AsyncSession, limit: int = 50) -> list[PaperTrade]:
    res = await session.execute(
        select(PaperTradeRow).order_by(PaperTradeRow.created_at.desc()).limit(limit)
    )
    return [PaperTrade.model_validate(r.payload) for r in res.scalars().all()]
