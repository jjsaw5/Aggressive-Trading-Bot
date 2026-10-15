"""On-demand single-symbol research (live symbol search) and trade evaluation.

Read-only with respect to the market: no orders are placed, ever. Suggested
plays route through the same engines and gates as everywhere else.

The two endpoints answer different questions. `/symbol/{symbol}` asks "what
should I look at?" and runs the engines under this account's limits.
`/evaluate` asks "here is a trade I am considering; what is wrong with it?" and
deliberately ignores budget, heat and position count — see
`app/engine/trade_evaluator.py`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.domain.evaluation import StructureType, TradeEvaluation
from app.domain.research import SymbolReport
from app.logging_config import get_logger
from app.research.evaluate import evaluate_trade
from app.research.symbol import build_symbol_report

router = APIRouter(prefix="/research", tags=["research"])
log = get_logger(__name__)


def _valid_symbol(symbol: str) -> str:
    sym = symbol.upper().strip()
    if not sym or len(sym) > 8 or not sym.isalpha():
        raise HTTPException(400, "Provide a valid stock symbol (letters only).")
    return sym


@router.get("/symbol/{symbol}", response_model=SymbolReport)
async def symbol_report(symbol: str) -> SymbolReport:
    """Live research report for one symbol: quote, intraday levels, flow, IV, news,
    catalysts, fundamentals, and suggested plays (0DTE / 1-5DTE / swing)."""
    return await build_symbol_report(_valid_symbol(symbol))


class EvaluateRequest(BaseModel):
    symbol: str
    structure: StructureType
    # "0d" / "3d" / "2w" / "45d", or an ISO date. Resolved against the LISTED
    # expirations and echoed back, because "3d" means a different contract
    # depending on the day it is asked.
    horizon: str = Field(..., min_length=1, max_length=16)
    # Optional. Supply them to grade YOUR structure and see it contrasted with
    # the selector's pick; omit them to have the tool build the best structure
    # it can at that expiry and grade that.
    long_strike: float | None = None
    short_strike: float | None = None


@router.post("/evaluate", response_model=TradeEvaluation)
async def evaluate(req: EvaluateRequest) -> TradeEvaluation:
    """Grade a proposed trade on construction — cost, modelled odds, execution
    cost, IV context, timing — with the account's risk limits out of scope.

    UNCALIBRATED. This is not a prediction of profit; see
    `docs/TRADE_EVALUATOR.md` for what each dimension does and does not claim.
    """
    symbol = _valid_symbol(req.symbol)
    if req.structure.is_spread and req.long_strike is not None and req.short_strike is None:
        raise HTTPException(400, "A debit spread needs both strikes, or neither.")
    if req.long_strike is None and req.short_strike is not None:
        raise HTTPException(400, "Supply the long strike too, or neither strike.")

    ev = await evaluate_trade(
        symbol=symbol, structure=req.structure, horizon=req.horizon,
        long_strike=req.long_strike, short_strike=req.short_strike,
    )

    # Persist to the evaluator's OWN table. Never `decision_snapshots` — these
    # are not signals the system generated, and a user re-evaluating one idea
    # repeatedly must not move the base rate the conviction gate is measured on.
    if settings.trade_eval_persist:
        try:
            import anyio

            from app.db.repository import save_trade_evaluation

            await anyio.to_thread.run_sync(
                save_trade_evaluation, ev, uuid.uuid4().hex[:32]
            )
        except Exception as exc:  # noqa: BLE001 - storing is not worth failing the answer
            log.warning("trade_evaluation_persist_failed", error=str(exc))
            ev.errors.setdefault("persist", str(exc)[:200])
    return ev
