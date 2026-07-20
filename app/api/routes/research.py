"""On-demand single-symbol research (live symbol search).

Read-only. Aggregates market context + activity + suggested plays for ANY ticker.
No orders are placed; suggested plays route through the same engines and gates as
everywhere else.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.domain.research import SymbolReport
from app.research.symbol import build_symbol_report

router = APIRouter(prefix="/research", tags=["research"])


@router.get("/symbol/{symbol}", response_model=SymbolReport)
async def symbol_report(symbol: str) -> SymbolReport:
    """Live research report for one symbol: quote, intraday levels, flow, IV, news,
    catalysts, fundamentals, and suggested plays (0DTE / 1-5DTE / swing)."""
    sym = symbol.upper().strip()
    if not sym or len(sym) > 8 or not sym.isalpha():
        raise HTTPException(400, "Provide a valid stock symbol (letters only).")
    return await build_symbol_report(sym)
