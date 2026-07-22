"""Reusable score components. Each returns a `ScoreComponent` (value in [0,1] +
explanation); the per-DTE models weight and combine them. A `None` value means
the input was unavailable — the model treats that as a low, flagged contribution,
never as a neutral pass.
"""

from __future__ import annotations

from app.domain.enums import Direction
from app.domain.options import IVContext, OptionChain, OptionContract
from app.engine.liquidity import OptionLiquidityConfig, option_liquidity_score
from app.engine.price_action import analyze_price_action
from app.shortduration.scoring.flow_decay import FlowAnalysis
from app.shortduration.scoring.models import NewsScore, ScoreComponent
from app.shortduration.strategies.base import SetupContext

_LIQ = OptionLiquidityConfig()


def price_structure(ctx: SetupContext, direction: Direction) -> ScoreComponent:
    lv = ctx.levels
    if lv is None or lv.last is None or lv.vwap is None:
        return ScoreComponent(value=None, explanation="No VWAP/level data.")
    hits, total, notes = 0, 0, []
    total += 1
    on_side = (lv.last > lv.vwap) if direction == Direction.BULLISH else (lv.last < lv.vwap)
    hits += 1 if on_side else 0
    notes.append(("above" if lv.last > lv.vwap else "below") + " VWAP")
    if lv.opening_range_high is not None:
        total += 1
        broke = (
            lv.last > lv.opening_range_high if direction == Direction.BULLISH
            else lv.last < (lv.opening_range_low or lv.opening_range_high)
        )
        hits += 1 if broke else 0
        notes.append("broke OR" if broke else "inside OR")
    return ScoreComponent(value=round(hits / total, 3), explanation=", ".join(notes))


def market_alignment(ctx: SetupContext, direction: Direction) -> ScoreComponent:
    r = ctx.regime
    from app.domain.enums import ShortDurationRegime as R

    contra = (
        (direction == Direction.BULLISH and r.regime == R.BEAR_TREND)
        or (direction == Direction.BEARISH and r.regime == R.BULL_TREND)
    )
    if contra:
        return ScoreComponent(value=0.1, explanation=f"Regime {r.regime.value} contradicts.")
    val = 0.5
    notes = [f"regime {r.regime.value}"]
    if r.breadth_above_vwap_pct is not None:
        bull_breadth = r.breadth_above_vwap_pct if direction == Direction.BULLISH else 1 - r.breadth_above_vwap_pct
        val = 0.3 + 0.6 * bull_breadth
        notes.append(f"breadth {int(r.breadth_above_vwap_pct * 100)}%>VWAP")
    if not r.allow_new_trades:
        val *= 0.5
        notes.append("new trades gated")
    return ScoreComponent(value=round(min(1.0, val), 3), explanation=", ".join(notes))


def relvol_momentum(ctx: SetupContext, direction: Direction) -> ScoreComponent:
    lv = ctx.levels
    parts, vals = [], []
    if lv is not None and lv.relative_volume is not None:
        rv = min(1.0, max(0.0, (lv.relative_volume - 1.0) / 2.0))
        vals.append(rv)
        parts.append(f"relvol {lv.relative_volume:g}x")
    if ctx.change_pct is not None:
        aligned = (ctx.change_pct > 0) if direction == Direction.BULLISH else (ctx.change_pct < 0)
        mom = min(1.0, abs(ctx.change_pct) / 2.0) if aligned else 0.0
        vals.append(mom)
        parts.append(f"move {ctx.change_pct:+.2f}%")
    if not vals:
        return ScoreComponent(value=None, explanation="No relvol/momentum data.")
    return ScoreComponent(value=round(sum(vals) / len(vals), 3), explanation=", ".join(parts))


def flow_quality(flow: FlowAnalysis | None, direction: Direction) -> ScoreComponent:
    if flow is None or flow.decayed_sentiment is None:
        return ScoreComponent(value=None, explanation="No flow.")
    aligned = (flow.decayed_sentiment > 0) if direction == Direction.BULLISH else (flow.decayed_sentiment < 0)
    val = flow.confidence if aligned else max(0.0, 0.2 - flow.confidence)
    if flow.repeated_strikes and aligned:
        val = min(1.0, val + 0.1)
    return ScoreComponent(value=round(val, 3), explanation=flow.explanation)


def _near_atm(chain: OptionChain, direction: Direction) -> OptionContract | None:
    spot = chain.underlying_price
    if spot is None or not chain.contracts:
        return None
    from app.domain.enums import OptionType

    want = OptionType.CALL if direction == Direction.BULLISH else OptionType.PUT
    cands = [c for c in chain.contracts if c.option_type == want]
    if not cands:
        cands = chain.contracts
    exp = min(c.expiration for c in cands)
    near = [c for c in cands if c.expiration == exp]
    return min(near, key=lambda c: abs(c.strike - spot))


def contract_liquidity(chain: OptionChain | None, direction: Direction) -> ScoreComponent:
    if chain is None or not chain.contracts:
        return ScoreComponent(value=None, explanation="No chain — refined in Phase 4.")
    c = _near_atm(chain, direction)
    if c is None:
        return ScoreComponent(value=None, explanation="No near-ATM contract.")
    score = option_liquidity_score(c, _LIQ)
    return ScoreComponent(
        value=round(score, 3),
        explanation=f"{c.strike:g}{c.option_type.value[0].upper()} spread "
        f"{'%.0f%%' % (c.spread_pct * 100) if c.spread_pct is not None else 'n/a'}, "
        f"OI {c.open_interest or 0}, vol {c.volume or 0}",
    )


def volatility_suitability(iv: IVContext | None) -> ScoreComponent:
    # Two INDEPENDENT reads of how rich IV is: iv_rank (min-max window, spike-
    # sensitive) and iv_percentile (distribution-based, robust). Averaging them is
    # steadier than either alone. Term structure and the rank source then adjust it.
    if iv is None or (iv.iv_rank is None and iv.iv_percentile is None):
        return ScoreComponent(value=None, explanation="No IV rank/percentile.")
    reads = [x for x in (iv.iv_rank, iv.iv_percentile) if x is not None]
    r = sum(reads) / len(reads)
    # Directional debit expression: cheap enough to buy, live enough to move.
    if 0.2 <= r <= 0.5:
        val = 1.0
    elif r < 0.2:
        val = 0.5 + r  # very low IV -> muted movement
    else:
        val = max(0.2, 1.0 - (r - 0.5))  # rich IV -> crush risk on debits
    parts = [f"IV level {r:.2f} ({len(reads)}-feature)"]
    # Backwardation (front IV richer than back, negative slope) warns of an event /
    # IV-crush ahead — a debit buyer pays up now and gets crushed on the print.
    if iv.term_structure_slope is not None and iv.term_structure_slope < -0.01:
        val *= 0.85
        parts.append("backwardated (crush risk)")
    # An HV-proxy "rank" is a realized-vol stand-in, NOT the traded IV surface —
    # discount it and label it so it is never mistaken for a true IV rank.
    if iv.iv_rank_source == "hv_proxy":
        val *= 0.85
        parts.append("HV-proxy, not true IV rank")
    if iv.iv_skew is not None:
        parts.append(f"skew {iv.iv_skew:+.2f}")
    return ScoreComponent(value=round(min(1.0, val), 3), explanation="; ".join(parts))


def catalyst_news(ctx: SetupContext, news: NewsScore | None) -> ScoreComponent:
    parts, val = [], 0.3
    if news is not None:
        val = max(val, news.total)
        parts.append(f"news {news.total:.2f}" + (" (dup)" if news.is_duplicate else ""))
    if ctx.catalysts:
        val = min(1.0, val + 0.2)
        parts.append(f"{len(ctx.catalysts)} catalyst(s)")
    if not parts:
        return ScoreComponent(value=0.3, explanation="No news/catalyst.")
    return ScoreComponent(value=round(val, 3), explanation=", ".join(parts))


def daily_trend(ctx: SetupContext, direction: Direction) -> ScoreComponent:
    if ctx.daily is None or len(ctx.daily.closes) < 50:
        return ScoreComponent(value=None, explanation="Insufficient daily history.")
    pa = analyze_price_action(ctx.daily)
    aligned = pa.direction == direction
    val = pa.score if aligned else max(0.0, 0.3 - pa.score)
    return ScoreComponent(value=round(val, 3), explanation=pa.rationale)


def multi_session_flow(flow: FlowAnalysis | None) -> ScoreComponent:
    if flow is None:
        return ScoreComponent(value=None, explanation="No flow.")
    val = min(1.0, 0.3 + 0.1 * flow.prints + (0.2 if flow.repeated_strikes else 0.0))
    if flow.opposing_present:
        val *= 0.6
    return ScoreComponent(value=round(val, 3), explanation=flow.explanation)


def risk_reward(ctx: SetupContext, direction: Direction, plan=None) -> ScoreComponent:
    # With a sized defined-risk plan (Phase 4), use the real reward-to-risk.
    if plan is not None and plan.risk is not None:
        rr = plan.risk.reward_to_risk
        if rr is not None:
            return ScoreComponent(value=round(min(1.0, rr / 2.0), 3),
                                  explanation=f"defined-risk R:R {rr:g}:1")
        # Long single option: upside open-ended, capped risk -> favorable but scenario-based.
        return ScoreComponent(value=0.65, explanation="long option — capped risk, open upside")
    # No plan yet: proximity-to-invalidation proxy.
    lv = ctx.levels
    if lv is None or lv.last is None or lv.vwap is None:
        return ScoreComponent(value=0.4, explanation="Provisional — awaiting a sized structure.")
    dist = abs(lv.last - lv.vwap) / lv.last if lv.last else 0.0
    val = min(0.8, 0.4 + dist * 10)
    return ScoreComponent(value=round(val, 3), explanation="Provisional (VWAP-distance proxy).")


def technical_entry(ctx: SetupContext, direction: Direction) -> ScoreComponent:
    lv = ctx.levels
    if lv is None or lv.above_vwap is None:
        return ScoreComponent(value=0.4, explanation="Limited intraday data.")
    on_side = lv.above_vwap if direction == Direction.BULLISH else (lv.above_vwap is False)
    return ScoreComponent(
        value=0.7 if on_side else 0.3,
        explanation="intraday aligned" if on_side else "intraday not yet aligned",
    )
