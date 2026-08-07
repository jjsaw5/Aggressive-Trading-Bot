"""Fetch what the trade evaluator needs, then score it.

Same best-effort fan-out shape as `app.research.symbol`: each provider call is
independent and a miss records an error for that section instead of failing the
request. The difference is what is NOT called — `run_detection`, which the
symbol report uses for its suggested plays and which persists to the capture
corpus as a side effect of being invoked. Evaluating a hypothetical trade must
not deposit rows in the warehouse of what the system believed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.domain.evaluation import StructureType, TradeEvaluation
from app.engine.trade_evaluator import CHAIN_EXPIRATIONS, EvaluationInputs, evaluate
from app.logging_config import get_logger
from app.providers import registry

log = get_logger(__name__)

_SECTION_TIMEOUT_S = 25.0


async def _guard(errors: dict[str, str], key: str, coro):
    try:
        return await asyncio.wait_for(coro, timeout=_SECTION_TIMEOUT_S)
    except TimeoutError:
        errors[key] = "timed out"
    except Exception as exc:  # noqa: BLE001 - one section must not kill the evaluation
        errors[key] = str(exc)[:200]
        log.warning("evaluation_section_failed", section=key, error=str(exc))
    return None


async def gather_inputs(symbol: str) -> tuple[EvaluationInputs, dict[str, str]]:
    """Pull chain, IV context, earnings and spot concurrently.

    The chain is the only hard requirement — without it there is no structure to
    price. The other three degrade individual dimensions to NOT_ASSESSED rather
    than failing the evaluation, which is the whole point of scoring dimensions
    independently.
    """
    errors: dict[str, str] = {}
    chain, iv, earnings, quote = await asyncio.gather(
        _guard(errors, "chain",
               registry.options_chain_provider().get_option_chain(
                   symbol, expirations=CHAIN_EXPIRATIONS)),
        _guard(errors, "iv", registry.options_chain_provider().get_iv_context(symbol)),
        _guard(errors, "earnings", registry.calendar_provider().get_earnings(symbol)),
        _guard(errors, "quote", registry.market_data_provider().get_quote(symbol)),
    )
    # Prefer the chain's own underlying price: it is the spot the option marks
    # were struck against, so break-even arithmetic stays internally consistent
    # even when the equity quote is a few seconds newer.
    spot = None
    if chain is not None and chain.underlying_price:
        spot = chain.underlying_price
    elif quote is not None and quote.price:
        spot = quote.price
    return EvaluationInputs(chain=chain, iv=iv, earnings=earnings, spot=spot), errors


async def evaluate_trade(
    *,
    symbol: str,
    structure: StructureType,
    horizon: str,
    long_strike: float | None = None,
    short_strike: float | None = None,
    now: datetime | None = None,
) -> TradeEvaluation:
    symbol = symbol.upper().strip()
    now = now or datetime.now(UTC)
    inputs, errors = await gather_inputs(symbol)
    ev = evaluate(
        symbol=symbol, structure=structure, horizon=horizon, inputs=inputs,
        long_strike=long_strike, short_strike=short_strike, now=now,
    )
    # Provider errors merge UNDER scoring errors: a section that failed to fetch
    # explains a NOT_ASSESSED dimension, and must not overwrite a more specific
    # message the scorer produced about the same key.
    for k, v in errors.items():
        ev.errors.setdefault(k, v)
    log.info(
        "trade_evaluated", symbol=symbol, structure=structure.value, horizon=horizon,
        grade=ev.grade or "none", assessed=ev.dimensions_assessed,
        total=ev.dimensions_total, errors=len(ev.errors),
    )
    return ev
