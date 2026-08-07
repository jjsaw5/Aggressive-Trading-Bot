"""A credential-free agent runner.

This exists so the pipeline runs end to end with no API key, and so the test
suite exercises the *real* validation path rather than a shortcut. It stands in
for model judgement with explicit heuristics.

**What it is not.** It is not a mock that returns canned market data. It reads
the same evidence ledger a model would, cites the same real ids, and refuses the
same claims. Its outputs go through exactly the same Pydantic parsing and
evidence binding as a model's JSON, so nothing downstream can tell the
difference — which is the point, because that is the code path that must be
correct.

**What it lacks.** Judgement. It classifies a catalyst by keyword, reads
direction from measured trend plus catalyst sentiment, and cannot weigh a
subtle argument. Every artifact it produces is stamped `runner=deterministic`
so a corpus built with it is never mistaken for a model-authored one.

The keyword tables below are heuristics standing in for an LLM. They are
**not** methodology: nothing here contributes a point to the composite score, so
they live with the implementation rather than in `config/methodology.yaml`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.multiagent.llm.runner import AgentInvocation, AgentResult, AgentRunner
from app.multiagent.models.enums import (
    BiasDirection,
    CatalystScope,
    CatalystType,
    Direction,
    EvidenceQuality,
    ExpectedDirection,
    Importance,
    MarketRegime,
    StrategyType,
    TimeHorizon,
    ValidationVerdict,
    VolatilityRegime,
)
from app.multiagent.models.evidence import EvidenceItem, EvidenceLedger

# --- Heuristic tables (stand-ins for model judgement, never scored) ----------

_CATALYST_KEYWORDS: tuple[tuple[CatalystType, tuple[str, ...]], ...] = (
    (CatalystType.EARNINGS, ("earnings", "quarterly results", "q1 ", "q2 ", "q3 ", "q4 ", "eps")),
    (CatalystType.GUIDANCE, ("guidance", "outlook", "forecast", "raises full-year", "cuts full-year")),
    (CatalystType.ANALYST_RATING, ("upgrade", "downgrade", "initiated coverage", "reiterated", "rating")),
    (CatalystType.PRICE_TARGET, ("price target", "pt raised", "pt cut")),
    (CatalystType.MERGER_ACQUISITION, ("merger", "acquisition", "acquire", "takeover", "buyout", "deal to buy")),
    (CatalystType.PRODUCT_LAUNCH, ("launch", "unveil", "announce", "introduces", "new chip", "new product")),
    (CatalystType.FDA_DECISION, ("fda", "pdufa", "phase 3", "phase iii", "clinical trial", "approval")),
    (CatalystType.LITIGATION, ("lawsuit", "litigation", "sues", "settlement", "court")),
    (CatalystType.REGULATORY, ("regulator", "antitrust", "probe", "investigation", "sanction", "export control")),
    (CatalystType.SEC_FILING, ("8-k", "10-q", "10-k", "s-1", "filing")),
    (CatalystType.EXECUTIVE_CHANGE, ("ceo", "cfo", "steps down", "resign", "appoint")),
    (CatalystType.CONTRACT_AWARD, ("contract", "order win", "awarded", "supply agreement")),
    (CatalystType.CPI, ("cpi", "consumer price")),
    (CatalystType.PPI, ("ppi", "producer price")),
    (CatalystType.PCE, ("pce", "personal consumption")),
    (CatalystType.GDP, ("gdp", "gross domestic")),
    (CatalystType.EMPLOYMENT, ("nonfarm", "payroll", "employment report", "unemployment rate")),
    (CatalystType.JOBLESS_CLAIMS, ("jobless claims", "initial claims")),
    (CatalystType.RETAIL_SALES, ("retail sales",)),
    (CatalystType.CONSUMER_CONFIDENCE, ("consumer confidence", "consumer sentiment")),
    (CatalystType.ISM, ("ism", "pmi", "purchasing managers")),
    (CatalystType.TREASURY_YIELDS, ("treasury", "yield", "10-year", "bond market")),
    (CatalystType.FOMC, ("fomc", "rate decision", "federal open market")),
    (CatalystType.FED_SPEAKER, ("powell", "fed speaker", "fed governor", "fed president")),
    (CatalystType.FED_MINUTES, ("fed minutes", "meeting minutes")),
    (CatalystType.GEOPOLITICAL, ("tariff", "sanctions", "war", "conflict", "trade dispute")),
    (CatalystType.SECTOR_ROTATION, ("rotation", "sector shift")),
    (CatalystType.DIVIDEND, ("dividend",)),
    (CatalystType.SPLIT, ("stock split",)),
)

_BULLISH_WORDS = (
    "beat", "beats", "raise", "raises", "raised", "upgrade", "upgraded", "surge",
    "jump", "rally", "record", "strong", "wins", "awarded", "approval", "approved",
    "outperform", "buy rating", "expands", "growth", "tops",
)
_BEARISH_WORDS = (
    "miss", "misses", "cut", "cuts", "downgrade", "downgraded", "plunge", "fall",
    "falls", "slump", "weak", "warn", "warns", "warning", "probe", "lawsuit",
    "recall", "halt", "delay", "sell rating", "underperform", "layoff", "loss",
)
_HIGH_IMPORTANCE_WORDS = (
    "fomc", "cpi", "earnings", "acquisition", "merger", "fda", "guidance",
    "payroll", "rate decision", "bankruptcy",
)


def _text_of(item: EvidenceItem) -> str:
    return f"{item.headline or ''} {item.summary or ''}".lower()


def _classify_catalyst(item: EvidenceItem) -> CatalystType:
    text = _text_of(item)
    for ctype, words in _CATALYST_KEYWORDS:
        if any(w in text for w in words):
            return ctype
    return CatalystType.OTHER


def _classify_direction(item: EvidenceItem) -> ExpectedDirection:
    text = _text_of(item)
    bull = sum(1 for w in _BULLISH_WORDS if w in text)
    bear = sum(1 for w in _BEARISH_WORDS if w in text)
    if bull > bear:
        return ExpectedDirection.BULLISH
    if bear > bull:
        return ExpectedDirection.BEARISH
    # A scheduled event with no released numbers implies magnitude, not direction.
    if item.kind.value in {"economic_event", "earnings_event", "calendar_catalyst"}:
        return ExpectedDirection.VOLATILE
    return ExpectedDirection.UNKNOWN


def _classify_importance(item: EvidenceItem) -> Importance:
    text = _text_of(item)
    if any(w in text for w in _HIGH_IMPORTANCE_WORDS):
        return Importance.HIGH
    if item.quality is EvidenceQuality.CONFIRMED_FACT:
        return Importance.MEDIUM
    return Importance.LOW


_IMPORTANCE_SCORE = {
    Importance.CRITICAL: 0.9,
    Importance.HIGH: 0.7,
    Importance.MEDIUM: 0.5,
    Importance.LOW: 0.3,
}


class DeterministicAgentRunner(AgentRunner):
    """Heuristic stand-in for model judgement. Cites real evidence only."""

    runner_id = "deterministic"

    async def run(self, invocation: AgentInvocation) -> AgentResult:
        key = invocation.definition.agent_key or invocation.definition.name.replace("-", "_")
        handler = {
            "market_intelligence": self._market_intelligence,
            "opportunity_generator": self._opportunity_generator,
            "trade_validator": self._trade_validator,
        }.get(key)
        if handler is None:
            return AgentResult(
                data=None,
                runner_id=self.runner_id,
                errors=[
                    f"deterministic runner has no handler for agent {key!r}; "
                    "configure an LLM-backed runner to use it"
                ],
            )
        data = handler(invocation.context)
        return AgentResult(data=data, runner_id=self.runner_id, raw_text="(deterministic runner)")

    # -- Agent 1 ----------------------------------------------------------
    def _market_intelligence(self, ctx: dict[str, Any]) -> dict[str, Any]:
        ledger: EvidenceLedger = ctx["ledger"]
        indices: dict[str, Any] = ctx.get("indices", {})
        now: datetime = ctx.get("now") or datetime.now(UTC)

        spy_bias = _bias_of(indices.get("SPY"))
        qqq_bias = _bias_of(indices.get("QQQ"))
        vol_regime: VolatilityRegime = ctx.get("volatility_regime", VolatilityRegime.UNKNOWN)

        regime = _regime_from(spy_bias, qqq_bias, vol_regime)

        company_catalysts: list[dict[str, Any]] = []
        news_items: list[dict[str, Any]] = []
        for item in ledger.narrative_items():
            ctype = _classify_catalyst(item)
            direction = _classify_direction(item)
            importance = _classify_importance(item)
            scope = (
                CatalystScope.MARKET
                if item.kind.value == "economic_event"
                else CatalystScope.COMPANY
                if item.symbol
                else CatalystScope.MARKET
            )
            if item.symbol:
                company_catalysts.append(
                    {
                        "ticker": item.symbol,
                        "catalyst_type": ctype.value,
                        "headline": item.headline or item.summary or ctype.value,
                        "description": item.summary,
                        "source": item.source,
                        "source_url": item.url,
                        "published_at": item.published_at.isoformat() if item.published_at else None,
                        "retrieved_at": item.retrieved_at.isoformat(),
                        "expected_direction": direction.value,
                        "importance": importance.value,
                        "importance_score": _IMPORTANCE_SCORE[importance],
                        "expected_time_horizon": _horizon_for(ctype).value,
                        "scheduled_event_date": _scheduled_date(item),
                        "is_scheduled": item.quality is EvidenceQuality.CONFIRMED_FACT,
                        "evidence_quality": item.quality.value,
                        "scope": scope.value,
                        "evidence_refs": [item.id],
                    }
                )
            news_items.append(
                {
                    "evidence_id": item.id,
                    "ticker": item.symbol,
                    "headline": item.headline or item.summary or "(no headline)",
                    "source": item.source,
                    "url": item.url,
                    "published_at": item.published_at.isoformat() if item.published_at else None,
                    "retrieved_at": item.retrieved_at.isoformat(),
                    "catalyst_type": ctype.value,
                    "scope": scope.value,
                    "relevance_confidence": _IMPORTANCE_SCORE[importance],
                    "why_relevant": f"classified {ctype.value} by keyword match",
                }
            )

        macro = [
            {
                "name": item.headline or item.summary or "economic event",
                "catalyst_type": _classify_catalyst(item).value,
                "scheduled_at": (item.published_at or item.retrieved_at).isoformat(),
                "is_scheduled": True,
                "importance": _classify_importance(item).value,
                "expected_direction": ExpectedDirection.VOLATILE.value,
                "consensus": item.payload.get("consensus"),
                "previous": item.payload.get("previous"),
                "actual": item.payload.get("actual"),
                "affected_markets": item.payload.get("affected_markets", []),
                "evidence_refs": [item.id],
                "notes": "",
            }
            for item in ledger.economic_items()
        ]
        upcoming = [m for m in macro if _is_future(m["scheduled_at"], now)]

        risk_events = [
            {
                "name": m["name"],
                "description": "scheduled high-impact release inside the horizon",
                "scheduled_at": m["scheduled_at"],
                "importance": m["importance"],
                "affected_symbols": m["affected_markets"],
                "evidence_refs": m["evidence_refs"],
            }
            for m in upcoming
            if m["importance"] in {Importance.HIGH.value, Importance.CRITICAL.value}
        ]

        gaps: list[str] = [f"{k}: {v}" for k, v in ledger.provider_errors.items()]
        if not company_catalysts:
            gaps.append("no company-level catalyst evidence retrieved")
        if indices.get("SPY") is None:
            gaps.append("SPY context unavailable — market alignment will abstain")

        return {
            "market_regime": regime.value,
            "volatility_regime": vol_regime.value,
            "spy_bias": spy_bias.value,
            "qqq_bias": qqq_bias.value,
            "macro_events": macro,
            "upcoming_scheduled_events": upcoming,
            "sector_observations": ctx.get("sector_observations", []),
            "company_catalysts": company_catalysts,
            "news_items": news_items,
            "risk_events": risk_events,
            "relevance_confidence": 0.5,
            "summary": (
                f"Deterministic brief: SPY {spy_bias.value}, QQQ {qqq_bias.value}, "
                f"volatility {vol_regime.value}, regime {regime.value}. "
                f"{len(company_catalysts)} evidenced company catalyst(s), "
                f"{len(upcoming)} upcoming scheduled macro event(s). "
                "Classification is keyword-based, not model judgement."
            ),
            "data_gaps": gaps,
        }

    # -- Agent 2 ----------------------------------------------------------
    def _opportunity_generator(self, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        brief: dict[str, Any] = ctx["brief"]
        trends: dict[str, BiasDirection] = ctx.get("trends", {})
        max_candidates: int = ctx.get("max_candidates", 10)
        allowed: set[str] = set(ctx.get("allowed_strategies", []))
        vol_regime: str = brief.get("volatility_regime", VolatilityRegime.UNKNOWN.value)

        # One candidate per ticker: the strongest evidenced catalyst wins.
        best: dict[str, dict[str, Any]] = {}
        for cat in brief.get("company_catalysts", []):
            ticker = str(cat["ticker"]).upper()
            prior = best.get(ticker)
            if prior is None or cat.get("importance_score", 0.0) > prior.get("importance_score", 0.0):
                best[ticker] = cat

        out: list[dict[str, Any]] = []
        for ticker, cat in sorted(
            best.items(), key=lambda kv: kv[1].get("importance_score", 0.0), reverse=True
        ):
            catalyst_dir = cat.get("expected_direction", ExpectedDirection.UNKNOWN.value)
            trend = trends.get(ticker, BiasDirection.UNKNOWN)

            direction = _resolve_direction(catalyst_dir, trend)
            if direction is None:
                # No identifiable reason it moves *in a direction*. The agent
                # definition says prefer no trade; so no trade.
                continue

            strategy = _choose_strategy(direction, vol_regime, allowed)
            if strategy is None:
                continue

            supporting = [
                {"summary": c["headline"], "evidence_refs": c["evidence_refs"]}
                for c in brief.get("company_catalysts", [])
                if str(c["ticker"]).upper() == ticker and c is not cat
            ][:3]

            out.append(
                {
                    "ticker": ticker,
                    "direction": direction.value,
                    "strategy_type": strategy.value,
                    "thesis": _thesis_text(ticker, cat["headline"], catalyst_dir, trend, direction),
                    "primary_catalyst": cat["headline"],
                    "primary_catalyst_refs": cat["evidence_refs"],
                    "supporting_catalysts": supporting,
                    "expected_holding_period": cat.get(
                        "expected_time_horizon", TimeHorizon.TWO_TO_FOUR_WEEKS.value
                    ),
                    "expected_move": {
                        # No magnitude claimed: the deterministic runner has no
                        # basis for one, and a guess would be a fabrication. The
                        # breakeven-reachability rule abstains as a result.
                        "magnitude_pct": None,
                        "direction_is_up": direction is Direction.BULLISH,
                        "rationale": "magnitude not claimed; deterministic runner does not estimate move size",
                    },
                    "technical_context": f"20-day trend measured as {trend.value}",
                    "invalidation_thesis": _invalidation_text(ticker, direction, cat["headline"]),
                    "known_risks": [r["name"] for r in brief.get("risk_events", [])][:4],
                    "earnings_date": cat.get("scheduled_event_date")
                    if cat.get("catalyst_type") == CatalystType.EARNINGS.value
                    else None,
                    "catalyst_date": cat.get("scheduled_event_date"),
                    "preliminary_quality": _quality_for(cat, trend),
                    "agent_reasoning_summary": (
                        "Deterministic selection: highest-importance evidenced catalyst per "
                        "ticker, direction resolved from catalyst sentiment and measured trend. "
                        "No magnitude estimate is claimed."
                    ),
                    "evidence_refs": cat["evidence_refs"],
                }
            )
            if len(out) >= max_candidates:
                break
        return out

    # -- Agent 3 ----------------------------------------------------------
    def _trade_validator(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Interpretation only — every number here was measured by code."""
        verdicts: dict[str, str] = ctx.get("category_verdicts", {})
        confirming: list[str] = ctx.get("confirming", [])
        disconfirming: list[str] = ctx.get("disconfirming", [])
        gaps: list[str] = ctx.get("data_gaps", [])

        values = list(verdicts.values())
        contradicts = values.count(ValidationVerdict.CONTRADICTS.value)
        confirms = values.count(ValidationVerdict.CONFIRMS.value)
        insufficient = values.count(ValidationVerdict.INSUFFICIENT_DATA.value)

        if not values or insufficient >= max(1, len(values) // 2 + 1):
            overall = ValidationVerdict.INSUFFICIENT_DATA
        elif contradicts > confirms:
            overall = ValidationVerdict.CONTRADICTS
        elif confirms > contradicts and contradicts == 0:
            overall = ValidationVerdict.CONFIRMS
        else:
            overall = ValidationVerdict.MIXED

        return {
            "overall_verdict": overall.value,
            "confirming_findings": confirming,
            "disconfirming_findings": disconfirming,
            "data_gaps": gaps,
            "agent_commentary": (
                f"Deterministic validation: {confirms} confirming, {contradicts} contradicting, "
                f"{insufficient} insufficient across {len(values)} categories. "
                "Verdicts are mechanical tallies of measured data, not judgement."
            ),
        }


# --- helpers ----------------------------------------------------------------


def _bias_of(index: Any) -> BiasDirection:
    if index is None:
        return BiasDirection.UNKNOWN
    bias = getattr(index, "bias", None)
    return bias if isinstance(bias, BiasDirection) else BiasDirection.UNKNOWN


def _regime_from(spy: BiasDirection, qqq: BiasDirection, vol: VolatilityRegime) -> MarketRegime:
    if spy is BiasDirection.UNKNOWN and qqq is BiasDirection.UNKNOWN:
        return MarketRegime.UNKNOWN
    if vol in {VolatilityRegime.STRESSED}:
        return MarketRegime.RISK_OFF
    if spy is BiasDirection.BULLISH and qqq is BiasDirection.BULLISH:
        return MarketRegime.TRENDING_UP
    if spy is BiasDirection.BEARISH and qqq is BiasDirection.BEARISH:
        return MarketRegime.TRENDING_DOWN
    if spy is not qqq and BiasDirection.UNKNOWN not in {spy, qqq}:
        return MarketRegime.ROTATIONAL
    return MarketRegime.RANGE_BOUND


def _horizon_for(ctype: CatalystType) -> TimeHorizon:
    if ctype in {CatalystType.EARNINGS, CatalystType.FOMC, CatalystType.CPI, CatalystType.FDA_DECISION}:
        return TimeHorizon.ONE_TO_THREE_DAYS
    if ctype in {CatalystType.ANALYST_RATING, CatalystType.PRICE_TARGET, CatalystType.GUIDANCE}:
        return TimeHorizon.ONE_WEEK
    return TimeHorizon.TWO_TO_FOUR_WEEKS


def _scheduled_date(item: EvidenceItem) -> str | None:
    raw = item.payload.get("event_date") or item.payload.get("report_date")
    return str(raw) if raw else None


def _is_future(iso: str | None, now: datetime) -> bool:
    if not iso:
        return False
    try:
        ts = datetime.fromisoformat(iso)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts >= now


def _resolve_direction(catalyst_dir: str, trend: BiasDirection) -> Direction | None:
    """Directional read, or None meaning "no trade".

    A catalyst with no direction and a flat trend gives no reason to expect a
    move one way, and the agent definition is explicit that no-trade beats a
    forced setup.
    """
    if catalyst_dir == ExpectedDirection.BULLISH.value:
        return Direction.BULLISH
    if catalyst_dir == ExpectedDirection.BEARISH.value:
        return Direction.BEARISH
    # Volatile/unknown catalyst: fall back to the measured trend, if there is one.
    if trend is BiasDirection.BULLISH:
        return Direction.BULLISH
    if trend is BiasDirection.BEARISH:
        return Direction.BEARISH
    return None


def _choose_strategy(
    direction: Direction, vol_regime: str, allowed: set[str]
) -> StrategyType | None:
    """Debit structure matched to direction and volatility.

    Elevated or stressed IV favours a spread over a single long option: the
    short leg finances part of the premium and reduces vega. That is a
    structural argument, not a prediction.
    """
    elevated = vol_regime in {VolatilityRegime.ELEVATED.value, VolatilityRegime.STRESSED.value}
    if direction is Direction.BULLISH:
        order = [StrategyType.BULL_CALL_SPREAD, StrategyType.LONG_CALL] if elevated else [
            StrategyType.LONG_CALL,
            StrategyType.BULL_CALL_SPREAD,
        ]
    else:
        order = [StrategyType.BEAR_PUT_SPREAD, StrategyType.LONG_PUT] if elevated else [
            StrategyType.LONG_PUT,
            StrategyType.BEAR_PUT_SPREAD,
        ]
    for s in order:
        if s.value in allowed:
            return s
    return None


def _thesis_text(
    ticker: str,
    headline: str,
    catalyst_dir: str,
    trend: BiasDirection,
    direction: Direction,
) -> str:
    """State plainly whether the trade runs with the measured trend or against it.

    The earlier wording claimed the trade "expresses the catalyst in the trend's
    direction" unconditionally, which was false whenever a bearish catalyst was
    traded against a bullish trend — exactly the case a reader most needs
    flagged. A thesis that misdescribes its own setup is worse than no thesis.
    """
    wants_up = direction is Direction.BULLISH
    if trend is BiasDirection.UNKNOWN:
        relation = "the 20-day trend could not be measured"
    elif (trend is BiasDirection.BULLISH) == wants_up and trend is not BiasDirection.NEUTRAL:
        relation = f"this runs WITH the measured {trend.value} 20-day trend"
    elif trend is BiasDirection.NEUTRAL:
        relation = "the 20-day trend is flat, so the trade leans on the catalyst alone"
    else:
        relation = (
            f"this runs AGAINST the measured {trend.value} 20-day trend — a counter-trend "
            "expression of the catalyst"
        )
    return (
        f"{ticker}: {headline}. Catalyst reads {catalyst_dir}; {relation}. "
        f"Direction taken: {direction.value}."
    )


def _invalidation_text(ticker: str, direction: Direction, headline: str) -> str:
    """A condition a human can actually check tomorrow."""
    side = "below" if direction is Direction.BULLISH else "above"
    return (
        f"Thesis fails if {ticker} closes {side} its price at validation for two consecutive "
        f"sessions, or if the catalyst ({headline[:60]}) is superseded or contradicted by "
        "later reporting."
    )


def _quality_for(cat: dict[str, Any], trend: BiasDirection) -> str:
    score = cat.get("importance_score", 0.0)
    aligned = trend is not BiasDirection.UNKNOWN
    if score >= 0.7 and aligned:
        return "strong"
    if score >= 0.5:
        return "moderate"
    return "speculative"
