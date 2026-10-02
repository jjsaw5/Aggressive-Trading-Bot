"""Fetch what the trade evaluator needs, then score it.

Same best-effort fan-out shape as `app.research.symbol`: each provider call is
independent and a miss records an error for that section instead of failing the
request. The difference is what is NOT called — `run_detection`, which the
symbol report uses for its suggested plays and which persists to the capture
corpus as a side effect of being invoked. Evaluating a hypothetical trade must
not deposit rows in the warehouse of what the system believed.

TWO JOINS THIS MODULE MUST NOT SKIP, both learned the hard way against live data:

1. **IV rank is BUILT, not fetched.** `get_iv_context` returns only the spot IV
   level; rank and percentile come from joining a real IV history through
   `build_iv_context`. Both scan paths (`shortduration/detection.py` and
   `engine/candidate_builder.py`) do this join. The first version of this module
   did not, so the evaluator reported `NA_no_data` for IV context on every
   symbol while the data sat one call away — 8 of 8 symbols probed had a null
   rank from the provider and a derivable one from 251 history points.

2. **Near-dated expiries need a targeted fetch.** `get_option_chain` picks the
   expirations NEAREST 30 DTE, so on a daily-expiry name the short-dated ones are
   sorted out of the window entirely: SPY's shortest reachable expiry was 7 DTE,
   which made the evaluator unable to price the 0DTE trades it is most often
   asked about. The horizon is therefore resolved FIRST and its neighbourhood
   fetched directly, then unioned with the default chain.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

from app.domain.evaluation import StructureType, TradeEvaluation
from app.domain.options import OptionChain
from app.engine.trade_evaluator import (
    CHAIN_EXPIRATIONS,
    EvaluationInputs,
    evaluate,
    parse_horizon,
)
from app.logging_config import get_logger
from app.providers import registry

log = get_logger(__name__)

_SECTION_TIMEOUT_S = 25.0

# How far either side of the requested horizon to probe for listed expiries.
# Each candidate date costs one provider request, so this is deliberately small:
# it exists to reach expiries the 30-DTE-centred default cannot see, not to
# enumerate the board.
_HORIZON_WINDOW_BEFORE = 3
_HORIZON_WINDOW_AFTER = 4
_MAX_PROBE_DATES = 8


async def _guard(errors: dict[str, str], key: str, coro):
    try:
        return await asyncio.wait_for(coro, timeout=_SECTION_TIMEOUT_S)
    except TimeoutError:
        errors[key] = "timed out"
    except Exception as exc:  # noqa: BLE001 - one section must not kill the evaluation
        errors[key] = str(exc)[:200]
        log.warning("evaluation_section_failed", section=key, error=str(exc))
    return None


def horizon_probe_dates(horizon: str, *, as_of: date) -> list[date]:
    """Candidate expiry dates around the requested horizon, never in the past.

    Returns [] when the horizon is unreadable — the caller then relies on the
    default chain alone and `resolve_expiration` reports the gap. Guessing a
    window for an unparseable horizon would probe dates for a trade nobody asked
    about.
    """
    target_days, explicit = parse_horizon(horizon)
    if explicit is not None:
        target = explicit
    elif target_days is not None:
        target = as_of + timedelta(days=target_days)
    else:
        return []
    lo = max(as_of, target - timedelta(days=_HORIZON_WINDOW_BEFORE))
    hi = target + timedelta(days=_HORIZON_WINDOW_AFTER)
    out: list[date] = []
    d = lo
    while d <= hi and len(out) < _MAX_PROBE_DATES:
        out.append(d)
        d += timedelta(days=1)
    return out


def merge_chains(primary: OptionChain | None, extra: OptionChain | None) -> OptionChain | None:
    """Union two chains, de-duplicated on (expiration, strike, type).

    `primary` wins on conflicts and supplies the underlying price, so the spot
    every break-even is computed against stays the one the default chain was
    struck at rather than flipping between two fetches.
    """
    if primary is None:
        return extra
    if extra is None:
        return primary
    seen = {(c.expiration, c.strike, c.option_type) for c in primary.contracts}
    merged = list(primary.contracts)
    merged.extend(
        c for c in extra.contracts
        if (c.expiration, c.strike, c.option_type) not in seen
    )
    return primary.model_copy(update={
        "contracts": merged,
        "underlying_price": primary.underlying_price or extra.underlying_price,
    })


async def gather_inputs(
    symbol: str, *, horizon: str = "", now: datetime | None = None
) -> tuple[EvaluationInputs, dict[str, str]]:
    """Pull chain, IV context, earnings and spot concurrently.

    The chain is the only hard requirement — without it there is no structure to
    price. The others degrade individual dimensions to NOT_ASSESSED rather than
    failing the evaluation, which is the whole point of scoring dimensions
    independently.
    """
    now = now or datetime.now(UTC)
    errors: dict[str, str] = {}
    chain_p = registry.options_chain_provider()
    iv_hist_p = registry.iv_history_provider()

    probe = horizon_probe_dates(horizon, as_of=now.date())
    calls = [
        _guard(errors, "chain",
               chain_p.get_option_chain(symbol, expirations=CHAIN_EXPIRATIONS)),
        _guard(errors, "iv", chain_p.get_iv_context(symbol)),
        _guard(errors, "earnings", registry.calendar_provider().get_earnings(symbol)),
        _guard(errors, "quote", registry.market_data_provider().get_quote(symbol)),
        _guard(errors, "price_history",
               registry.market_data_provider().get_price_history(symbol, lookback_days=365)),
        _guard(errors, "iv_history",
               iv_hist_p.get_iv_history(symbol, lookback_days=365))
        if iv_hist_p is not None else _none(),
        _guard(errors, "near_chain", chain_p.get_option_chain_for_expirations(symbol, probe))
        if probe else _none(),
    ]
    chain, iv, earnings, quote, price_history, iv_history, near_chain = await asyncio.gather(*calls)

    # (2) Union in the horizon's own neighbourhood so a short-dated expiry the
    # 30-DTE-centred default cannot reach is still priceable.
    chain = merge_chains(chain, near_chain)

    # (1) Build the IV context the way both scan paths do, so rank is consistent
    # app-wide. `build_iv_context` falls back to an HV proxy when there is no IV
    # history and labels which it used in `iv_rank_source` — modelled is labeled.
    if iv is not None or iv_history is not None or price_history is not None:
        from app.engine.iv_context import build_iv_context

        built = build_iv_context(
            symbol,
            iv.iv30 if iv is not None else None,
            now,
            iv_history=iv_history,
            price_history=price_history,
            term_structure_slope=iv.term_structure_slope if iv is not None else None,
        )
        # Keep the provider's skew if it supplied one; the builder has no chain.
        if iv is not None and iv.iv_skew is not None and built.iv_skew is None:
            built.iv_skew = iv.iv_skew
        iv = built

    # Prefer the chain's own underlying price: it is the spot the option marks
    # were struck against, so break-even arithmetic stays internally consistent
    # even when the equity quote is a few seconds newer.
    spot = None
    if chain is not None and chain.underlying_price:
        spot = chain.underlying_price
    elif quote is not None and quote.price:
        spot = quote.price
    return EvaluationInputs(chain=chain, iv=iv, earnings=earnings, spot=spot), errors


async def _none():
    return None


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
    inputs, errors = await gather_inputs(symbol, horizon=horizon, now=now)
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
