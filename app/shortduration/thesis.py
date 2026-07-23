"""Directional-thesis builder — a plain-English, deterministic "why this direction".

Assembles the same numbers the scanner scores (daily SMA20/50 trend, price-vs-mean,
RSI, today's move, the invalidation level) into a short human-readable thesis, plus
an INFORMATIONAL reversal-risk flag. It never gates or scores a trade — it exists so
a person can sanity-check a setup (e.g. a fresh bearish call on a big green day)
before acting. No LLM: every sentence maps to a signal you can verify.
"""

from __future__ import annotations

from datetime import timedelta

from app.config import get_settings
from app.domain.enums import Direction, DTECategory, ShortDurationStrategy
from app.domain.shortduration import DirectionalThesis, SourcedClaim
from app.engine.price_action import analyze_price_action
from app.shortduration.strategies.base import SetupContext, StrategyDetection

# The three joined providers. A source with no usable data for a symbol appears in
# sources_missing — the thesis names its silence instead of papering over it.
_SOURCES = ("fmp", "unusual_whales", "benzinga")


def _sourced_claims(ctx: SetupContext, pa, d: Direction) -> list[SourcedClaim]:
    """Join FMP (price/trend/earnings), Unusual Whales (flow), and Benzinga (news)
    into per-claim receipts. Every sentence carries source + field + raw value +
    timestamp. Observational sources say so — flow explicitly carries the
    no-validated-edge disclaimer (the registry's verdict travels with the claim)."""
    claims: list[SourcedClaim] = []

    # --- FMP: daily trend structure + today's move + next earnings ---
    if pa is not None and pa.details:
        sma20, sma50 = pa.details.get("sma20"), pa.details.get("sma50")
        rsi = pa.details.get("rsi14")
        if sma20 and sma50:
            trend = "downtrend" if sma20 < sma50 else "uptrend"
            claims.append(SourcedClaim(
                text=f"Daily {trend}: SMA20 {sma20:g} vs SMA50 {sma50:g} over the last year of closes.",
                source="fmp", field="daily.sma20_vs_sma50", value=f"{sma20:g}/{sma50:g}",
                as_of=ctx.daily.candles[-1].ts if (ctx.daily and ctx.daily.candles) else None,
            ))
        if rsi is not None:
            claims.append(SourcedClaim(
                text=f"RSI(14) {rsi:.0f} — {'weak' if rsi < 50 else 'firm'} momentum.",
                source="fmp", field="daily.rsi14", value=f"{rsi:.1f}",
                as_of=ctx.daily.candles[-1].ts if (ctx.daily and ctx.daily.candles) else None,
            ))
    if ctx.change_pct is not None:  # computed from the FMP quote vs prior close
        claims.append(SourcedClaim(
            text=f"{ctx.change_pct:+.2f}% today vs prior close.",
            source="fmp", field="quote.change_pct", value=f"{ctx.change_pct:+.2f}%",
            as_of=getattr(ctx.quote, "as_of", None) if ctx.quote is not None else None,
        ))
    if ctx.next_earnings is not None:
        claims.append(SourcedClaim(
            text=f"Next earnings {ctx.next_earnings}.",
            source="fmp", field="calendar.next_earnings", value=str(ctx.next_earnings),
        ))

    # --- Unusual Whales: today's flow tape (OBSERVATIONAL — validated null) ---
    if ctx.flow:
        prem = [a.premium for a in ctx.flow if a.premium]
        bull = sum(a.premium or 0 for a in ctx.flow if (a.sentiment or 0) > 0)
        tot = sum(prem)
        lean = f"{bull / tot:.0%} bullish by premium" if tot > 0 else "premium unknown"
        claims.append(SourcedClaim(
            text=(
                f"{len(ctx.flow)} unusual-flow prints today, {lean}. Observational only — "
                "no flow encoding has validated out-of-sample (see the feature registry); "
                "this describes the tape, it predicts nothing."
            ),
            source="unusual_whales", field="flow.today",
            value=f"n={len(ctx.flow)}, premium=${tot:,.0f}", as_of=ctx.now,
        ))

    # --- Benzinga: the most recent headline for this symbol ---
    sym_news = [n for n in (ctx.news or []) if (n.symbol or "").upper() == ctx.symbol.upper()]
    if sym_news:
        top = max(sym_news, key=lambda n: n.source_ts or n.received_ts)
        claims.append(SourcedClaim(
            text=f'Latest headline: "{top.headline}"',
            source="benzinga", field="news.headline", value=top.headline,
            as_of=top.source_ts or top.received_ts,
        ))

    return claims

# Strategies whose thesis is a multi-week/daily-trend swing — they need room to work.
_SWING_STRATEGIES = {ShortDurationStrategy.TREND_CONTINUATION}


def _structural_warnings(ctx: SetupContext, detection: StrategyDetection, s) -> list[str]:
    """Wrong-instrument guardrails (informational): is the thesis horizon compatible
    with the expiry, and does an earnings report fall before it? Learned from a
    daily-trend TSLA signal expressed in a ~4-DTE spread straddling earnings."""
    warnings: list[str] = []
    dte = detection.dte_category
    max_dte = 1 if dte == DTECategory.ZERO_DTE else s.short_duration_max_dte

    # Horizon mismatch: a swing thesis can't be expressed in a 0-5DTE expiry.
    if detection.strategy in _SWING_STRATEGIES and max_dte < s.thesis_swing_min_dte:
        warnings.append(
            f"Horizon mismatch: a daily-trend thesis needs ~{s.thesis_swing_min_dte}+ DTE to work, "
            f"but this {dte.value} expiry is ≤{max_dte} DTE — express it as a 20–45 DTE structure "
            f"in the core scanner, not a sub-week expiry."
        )
    # Earnings before expiry: turns a continuation trade into an event binary.
    if ctx.next_earnings is not None:
        horizon_end = ctx.now.date() + timedelta(days=max_dte)
        if ctx.now.date() <= ctx.next_earnings <= horizon_end:
            warnings.append(
                f"Earnings {ctx.next_earnings} land before expiry — this is an event binary "
                f"(IV-crush + gap risk), not a continuation trade. The thesis can be right and "
                f"still lose on the print."
            )
    return warnings


def _current_price(ctx: SetupContext, pa_price: float | None) -> float | None:
    if ctx.levels is not None and ctx.levels.last is not None:
        return ctx.levels.last
    if ctx.quote is not None and getattr(ctx.quote, "price", None):
        return ctx.quote.price
    return pa_price


def _headline(d: Direction, pa, chg: float | None) -> str:
    if pa is not None and pa.direction != Direction.NEUTRAL and pa.details:
        sma20 = pa.details.get("sma20")
        sma50 = pa.details.get("sma50")
        dist = pa.details.get("dist_from_sma20")
        rsi = pa.details.get("rsi14")
        trend = "downtrend" if (sma20 and sma50 and sma20 < sma50) else "uptrend"
        parts = [f"{d.value.capitalize()} — daily {trend}"]
        if dist is not None:
            parts.append(f"price {abs(dist) * 100:.1f}% {'below' if dist < 0 else 'above'} the 20-day mean")
        if rsi is not None:
            parts.append(f"RSI {rsi:.0f}")
        return ", ".join(parts) + "."
    return f"{d.value.capitalize()} intraday setup."


def _todays_context(d: Direction, chg: float | None, pa) -> str:
    if chg is None:
        return ""
    counter = (chg > 0 and d == Direction.BEARISH) or (chg < 0 and d == Direction.BULLISH)
    move = f"{chg:+.1f}% today"
    if not counter:
        return f"{move} — a move in the {d.value} direction (with the thesis)."
    dist = pa.details.get("dist_from_sma20") if (pa is not None and pa.details) else None
    if dist is not None:
        avg = "a falling" if d == Direction.BEARISH else "a rising"
        side = "below" if dist < 0 else "above"
        return (
            f"{move}: a bounce toward {avg} 20-day average, but price is still "
            f"{abs(dist) * 100:.1f}% {side} it — the {d.value} thesis is intact; this is the risk to watch."
        )
    return f"{move}: a counter-trend move against the {d.value} thesis — watch the invalidation."


def _reversal_risk(
    s, d: Direction, chg: float | None, dist_inval: float | None, news_score
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if chg is not None:
        counter = (chg > 0 and d == Direction.BEARISH) or (chg < 0 and d == Direction.BULLISH)
        if counter and abs(chg) >= s.thesis_reversal_counter_move_pct:
            reasons.append(f"Today's {chg:+.1f}% move is against the {d.value} thesis.")
    if dist_inval is not None and dist_inval <= s.thesis_reversal_near_invalidation_pct:
        reasons.append(f"Price is only {dist_inval:.1f}% from the invalidation level.")
    if news_score is not None and (news_score.total or 0) >= s.thesis_reversal_news_min_score:
        opp = (
            news_score.direction is not None
            and news_score.direction not in (d, Direction.NEUTRAL)
        )
        reasons.append("A material news catalyst is active today" + (" (opposing the trade)." if opp else "."))
    n = len(reasons)
    return ("high" if n >= 2 else "elevated" if n == 1 else "low"), reasons


def build_directional_thesis(
    ctx: SetupContext, detection: StrategyDetection, *, news_score=None
) -> DirectionalThesis:
    s = get_settings()
    d = detection.direction
    dte = detection.dte_category
    is_bear = d == Direction.BEARISH

    pa = analyze_price_action(ctx.daily) if ctx.daily is not None else None
    price = _current_price(ctx, pa.details.get("price") if (pa and pa.details) else None)

    drivers: list[str] = []
    invalidation_price: float | None = None
    invalidation = detection.invalidation or ""

    if pa is not None and pa.details:
        sma20 = pa.details.get("sma20")
        sma50 = pa.details.get("sma50")
        rsi = pa.details.get("rsi14")
        dist = pa.details.get("dist_from_sma20")
        if sma20 and sma50:
            structure = "downtrend (20-day below 50-day)" if sma20 < sma50 else "uptrend (20-day above 50-day)"
            drivers.append(f"Daily {structure}: SMA20 {sma20:g} vs SMA50 {sma50:g}.")
        if dist is not None:
            drivers.append(f"Price {abs(dist) * 100:.1f}% {'below' if dist < 0 else 'above'} the 20-day mean.")
        if rsi is not None:
            drivers.append(f"RSI {rsi:.0f} — {'weak' if rsi < 50 else 'firm'} momentum.")
        if dte != DTECategory.ZERO_DTE and sma20 is not None:
            invalidation_price = float(sma20)
            invalidation = f"Daily close back {'above' if is_bear else 'below'} SMA20 ({sma20:g})."

    if invalidation_price is None and ctx.levels is not None and ctx.levels.vwap is not None:
        invalidation_price = float(ctx.levels.vwap)
        if not invalidation:
            invalidation = f"Intraday close back {'above' if is_bear else 'below'} VWAP ({ctx.levels.vwap:g})."

    dist_inval = None
    if invalidation_price and price:
        dist_inval = round(abs(invalidation_price - price) / price * 100, 2)

    headline = _headline(d, pa, ctx.change_pct)
    todays_context = _todays_context(d, ctx.change_pct, pa)
    risk, risk_reasons = _reversal_risk(s, d, ctx.change_pct, dist_inval, news_score)

    # Full "read before you act" paragraph.
    structural = _structural_warnings(ctx, detection, s)

    bits = [headline]
    if todays_context:
        bits.append(todays_context)
    if invalidation:
        near = f" (price is {dist_inval:.1f}% away)" if dist_inval is not None else ""
        bits.append(f"Invalidation: {invalidation}{near}")
    if risk != "low":
        bits.append(f"Reversal risk {risk.upper()} — " + " ".join(risk_reasons))
    for w in structural:
        bits.append(f"⚠ {w}")
    summary = " ".join(bits)

    # Source joins: every claim with its receipt, and an honest account of which
    # providers contributed vs were silent for this symbol.
    claims = _sourced_claims(ctx, pa, d)
    used = sorted({c.source for c in claims} - {"computed"})
    missing = [s_ for s_ in _SOURCES if s_ not in used]

    return DirectionalThesis(
        direction=d, headline=headline, drivers=drivers, todays_context=todays_context,
        invalidation=invalidation, invalidation_price=invalidation_price,
        distance_to_invalidation_pct=dist_inval, reversal_risk=risk,
        reversal_risk_reasons=risk_reasons, structural_warnings=structural, summary=summary,
        claims=claims, sources_used=used, sources_missing=missing,
    )
