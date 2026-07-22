"""Data-quality score for a short-duration candidate.

Missing or stale data is never silently treated as neutral — it lowers data
quality, which in turn tempers the candidate's overall confidence. The score
reports exactly which inputs were present, missing, or stale so a low-quality
candidate is visibly low-quality, not quietly wrong.
"""

from __future__ import annotations

from datetime import datetime

from app.domain.enums import DTECategory
from app.domain.options import IVContext, OptionChain
from app.shortduration.scoring.models import ScoreComponent
from app.shortduration.strategies.base import SetupContext

_QUOTE_STALE_SECONDS = 120.0


def compute_data_quality(
    ctx: SetupContext, *, chain: OptionChain | None, iv: IVContext | None, dte: DTECategory
) -> ScoreComponent:
    checks: list[tuple[str, bool]] = []
    missing: list[str] = []

    def check(label: str, ok: bool) -> None:
        checks.append((label, ok))
        if not ok:
            missing.append(label)

    # Common inputs.
    check("intraday bars", bool(ctx.bars_1m))
    check("VWAP", ctx.levels is not None and ctx.levels.vwap is not None)
    fresh_quote = False
    if ctx.quote is not None and ctx.quote.as_of is not None:
        age = (ctx.now - ctx.quote.as_of).total_seconds()
        fresh_quote = ctx.quote.delayed_minutes == 0 and age <= _QUOTE_STALE_SECONDS
    check("fresh quote", fresh_quote)  # a missing timestamp is NOT fresh
    check("option liquidity (chain)", chain is not None and bool(chain.contracts))
    check("IV context", iv is not None and iv.iv_rank is not None)

    if dte == DTECategory.ZERO_DTE:
        check("opening range", ctx.levels is not None and ctx.levels.opening_range_high is not None)
        check("options flow", bool(ctx.flow))
    else:
        check("daily history", ctx.daily is not None and len(ctx.daily.closes) >= 50)

    value = round(sum(1 for _, ok in checks if ok) / len(checks), 3) if checks else 0.0
    expl = "all inputs present" if not missing else "missing/stale: " + ", ".join(missing)
    return ScoreComponent(value=value, explanation=expl)


def quote_is_stale(ctx: SetupContext, now: datetime | None = None) -> bool:
    if ctx.quote is None or ctx.quote.as_of is None:
        return True  # no quote, or no usable timestamp -> treat as stale
    now = now or ctx.now
    age = (now - ctx.quote.as_of).total_seconds()
    return ctx.quote.delayed_minutes > 0 or age > _QUOTE_STALE_SECONDS
