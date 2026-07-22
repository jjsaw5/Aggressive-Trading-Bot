"""Short-duration scoring engine.

Assembles the two SEPARATE weighted models (0DTE and 1-5DTE) from the shared
components, and surfaces the named sub-scores + a data-quality-tempered overall
confidence. A missing component is scored LOW and flagged (never treated as a
neutral pass), and independently lowers data quality — so an under-informed
candidate is conservatively scored and visibly so.
"""

from __future__ import annotations

from app.config import get_settings
from app.domain.enums import Direction, DTECategory
from app.domain.options import IVContext, OptionChain
from app.shortduration.scoring import components as C
from app.shortduration.scoring.data_quality import compute_data_quality
from app.shortduration.scoring.flow_decay import FlowAnalysis, analyze_flow
from app.shortduration.scoring.models import FactorScore, NewsScore, ScoreCard, ScoreComponent
from app.shortduration.strategies.base import SetupContext, StrategyDetection

_MISSING_RAW = 0.25  # a missing factor is scored low + flagged, not neutral

_ZERO_DTE = "0dte"
_SHORT_DTE = "1-5dte"

# Human labels for each factor key. Weights themselves are configurable + versioned
# (settings.scoring_0dte_weights / scoring_1_5dte_weights); the ordered key list per
# model is fixed here so a weight config can't silently drop or reorder a factor.
_LABELS: dict[str, str] = {
    "price_structure": "Intraday price structure",
    "market_alignment": "Market & sector alignment",
    "relvol_momentum": "Relative volume & momentum",
    "flow_quality": "Options-flow quality",
    "contract_liquidity": "Contract liquidity",
    "volatility": "Volatility suitability",
    "catalyst_news": "Catalyst & news",
    "risk_reward": "Risk/reward & execution",
    "daily_trend": "Daily & intraday trend",
    "multi_session_flow": "Multi-session flow",
    "technical_entry": "Technical-entry quality",
}
_ZERO_DTE_KEYS = (
    "price_structure", "market_alignment", "relvol_momentum", "flow_quality",
    "contract_liquidity", "volatility", "catalyst_news", "risk_reward",
)
_SHORT_DTE_KEYS = (
    "daily_trend", "catalyst_news", "multi_session_flow", "market_alignment",
    "volatility", "contract_liquidity", "technical_entry", "risk_reward",
)


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
    settings = get_settings()

    # Shared components, keyed for lookup against the configured weights.
    comps: dict[str, ScoreComponent] = {
        "price_structure": C.price_structure(ctx, d),
        "market_alignment": C.market_alignment(ctx, d),
        "relvol_momentum": C.relvol_momentum(ctx, d),
        "flow_quality": C.flow_quality(flow, d),
        "contract_liquidity": C.contract_liquidity(chain, d),
        "volatility": C.volatility_suitability(iv),
        "catalyst_news": C.catalyst_news(ctx, news_score),
        "risk_reward": C.risk_reward(ctx, d, trade_plan),
        "daily_trend": C.daily_trend(ctx, d),
        "multi_session_flow": C.multi_session_flow(flow),
        "technical_entry": C.technical_entry(ctx, d),
    }
    c_flow = comps["flow_quality"]
    c_liq = comps["contract_liquidity"]
    c_market = comps["market_alignment"]
    c_rr = comps["risk_reward"]
    dq = compute_data_quality(ctx, chain=chain, iv=iv, dte=dte)

    if dte == DTECategory.ZERO_DTE:
        keys, weights, cat = _ZERO_DTE_KEYS, settings.scoring_0dte_weights, _ZERO_DTE
    else:
        keys, weights, cat = _SHORT_DTE_KEYS, settings.scoring_1_5dte_weights, _SHORT_DTE
    factors = [
        _factor(k, _LABELS[k], float(weights.get(k, 0.0)), comps[k]) for k in keys
    ]

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

    # Honest degrade (Conviction-Scanner spec §6): no conviction feature has cleared
    # the validation gate and no live calibration exists, so this number is a
    # hand-weighted TRADABILITY rank, not calibrated conviction. Say so. And when IV
    # is absent the probability-of-profit is uncomputable — that must never read as
    # high conviction (it's the blank-POP case).
    pop_available = comps["volatility"].value is not None
    conviction_status = "UNCALIBRATED"
    if not pop_available:
        conviction_note = (
            "POP uncomputable (no IV) — tradability rank only; this is a clean "
            "structure, not a predicted winner. Your thesis required."
        )
    else:
        conviction_note = (
            "Hand-weighted tradability rank, not validated/calibrated conviction "
            "(no feature has cleared the net-of-cost validation gate). Your thesis required."
        )
    stamp = "UNCALIBRATED" + ("" if pop_available else " · POP unknown")
    summary = (
        f"[{stamp}] {cat} tradability {total:.0f}/100 (data {data_quality:.2f}). "
        f"Led by {top[0].label} & {top[1].label}. Not calibrated conviction."
    )
    return ScoreCard(
        dte_category=cat, total=total, overall_confidence=overall, factors=factors,
        components=components, data_quality=round(data_quality, 3), summary=summary,
        model_version=settings.scoring_model_version,
        risk_policy_version=settings.risk_policy_version,
        weights={k: float(weights.get(k, 0.0)) for k in keys},
        conviction_status=conviction_status, pop_available=pop_available,
        conviction_note=conviction_note,
    )
