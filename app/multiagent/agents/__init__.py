"""Agent wrappers.

Each wrapper builds the prompt, invokes the runner, and — critically — binds the
result to the evidence ledger before anything downstream sees it. Nothing an
agent returns reaches a report or a score without passing through
`app.multiagent.agents.binding`.
"""

from app.multiagent.agents.binding import (
    BindingResult,
    bind_claims,
    restrict_to_known_symbols,
)
from app.multiagent.agents.market_intelligence import run_market_intelligence
from app.multiagent.agents.opportunity_generator import run_opportunity_generator
from app.multiagent.agents.trade_validator import (
    SymbolData,
    build_measured_report,
    fetch_symbol_data,
    run_trade_validator,
)

__all__ = [
    "BindingResult",
    "SymbolData",
    "bind_claims",
    "build_measured_report",
    "fetch_symbol_data",
    "restrict_to_known_symbols",
    "run_market_intelligence",
    "run_opportunity_generator",
    "run_trade_validator",
]
