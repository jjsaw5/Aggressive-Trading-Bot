"""Short-duration scoring engine.

Assembles the two SEPARATE weighted models (0DTE and 1-5DTE) from the shared
components, and surfaces the named sub-scores + a data-quality-tempered overall
confidence. A missing component is scored LOW and flagged (never treated as a
neutral pass), and independently lowers data quality — so an under-informed
candidate is conservatively scored and visibly so.
"""

from __future__ import annotations

from app.domain.enums import Direction, DTECategory
from app.domain.options import IVContext, OptionChain
from app.shortduration.scoring import components as C
from app.shortduration.scoring.data_quality import compute_data_quality
from app.shortduration.scoring.flow_decay import FlowAnalysis, analyze_flow
from app.shortduration.scoring.models import FactorScore, NewsScore, ScoreCard, ScoreComponent
from app.shortduration.strategies.base import SetupContext, StrategyDetection

_MISSING_RAW = 0.25  # a missing factor is scored low + flagged, not neutral

# (key, label, weight, component-getter). Weights sum to 100 per model.
_ZERO_DTE = "0dte"
_SHORT_DTE = "1-5dte"


def _factor(key: str, label: str, weight: float, comp: ScoreComponent) -> FactorScore:
    raw = comp.value if comp.value is not None else _MISSING_RAW
    expl = comp.explanation if comp.value is not None else f"(no data) {comp.explanation}".strip()
    return FactorScore(
        key=key, label=label, weight=weight, raw=round(raw, 4),
        points=round(raw * weight, 2), explanation=expl,
    )


def score_candidate(
    ctx: SetupContext,
    detection: StrategyDetection,
    *,
    chain: OptionChain | None = None,
    iv: IVContext | None = None,
    news_score: NewsScore | None = None,
    flow_analysis: FlowAnalysis | None = None,
    trade_plan=None,
) -> ScoreCard:
    d: Direction = detection.direction
    flow = flow_analysis or analyze_flow(ctx.flow, ctx.now, d)
    dte = detection.dte_category

    # Shared components.
    c_price = C.price_structure(ctx, d)
    c_market = C.market_alignment(ctx, d)
    c_relvol = C.relvol_momentum(ctx, d)
    c_flow = C.flow_quality(flow, d)
    c_liq = C.contract_liquidity(chain, d)
    c_vol = C.volatility_suitability(iv)
    c_cat = C.catalyst_news(ctx, news_score)
    c_rr = C.risk_reward(ctx, d, trade_plan)
    c_daily = C.daily_trend(ctx, d)
    c_msflow = C.multi_session_flow(flow)
    c_tech = C.technical_entry(ctx, d)
    dq = compute_data_quality(ctx, chain=chain, iv=iv, dte=dte)

    if dte == DTECategory.ZERO_DTE:
        factors = [
            _factor("price_structure", "Intraday price structure", 20, c_price),
            _factor("market_alignment", "Market & sector alignment", 15, c_market),
            _factor("relvol_momentum", "Relative volume & momentum", 15, c_relvol),
            _factor("flow_quality", "Options-flow quality", 15, c_flow),
            _factor("contract_liquidity", "Contract liquidity", 15, c_liq),
            _factor("volatility", "Volatility suitability", 10, c_vol),
            _factor("catalyst_news", "Catalyst & news", 5, c_cat),
            _factor("risk_reward", "Risk/reward & execution", 5, c_rr),
        ]
        cat = _ZERO_DTE
    else:
        factors = [
            _factor("daily_trend", "Daily & intraday trend", 20, c_daily),
            _factor("catalyst_news", "Catalyst & news", 15, c_cat),
            _factor("multi_session_flow", "Multi-session flow", 15, c_msflow),
            _factor("market_alignment", "Market & sector alignment", 10, c_market),
            _factor("volatility", "Volatility suitability", 10, c_vol),
            _factor("contract_liquidity", "Contract liquidity", 10, c_liq),
            _factor("technical_entry", "Technical-entry quality", 10, c_tech),
            _factor("risk_reward", "Risk/reward", 10, c_rr),
        ]
        cat = _SHORT_DTE

    total = round(sum(f.points for f in factors), 2)
    normalized = total / 100.0
    data_quality = dq.value or 0.0
    overall = round(normalized * (0.6 + 0.4 * data_quality), 4)

    components = {
        "data_quality": dq,
        "liquidity": c_liq,
        "news_confidence": ScoreComponent(
            value=news_score.total if news_score else None,
            explanation=news_score.explanation if news_score else "no news",
        ),
        "flow_confidence": c_flow,
        "market_alignment": c_market,
        "execution_quality": ScoreComponent(
            value=c_liq.value, explanation=f"from contract liquidity — {c_liq.explanation}"
        ),
        "risk_quality": c_rr,
    }
    top = sorted(factors, key=lambda f: f.points, reverse=True)[:2]
    summary = (
        f"{cat} score {total:.0f}/100 (conf {overall:.2f}, data {data_quality:.2f}). "
        f"Led by {top[0].label} & {top[1].label}."
    )
    return ScoreCard(
        dte_category=cat, total=total, overall_confidence=overall, factors=factors,
        components=components, data_quality=round(data_quality, 3), summary=summary,
    )
