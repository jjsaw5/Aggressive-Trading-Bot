"""Agent 3 wrapper: measure everything, then ask the agent to disbelieve it.

The order matters and is the design. Python fetches fresh data, computes all six
snapshot categories and selects contracts. Only then does the agent see the
measurements, and its entire contribution is interpretation: verdicts,
confirming and disconfirming findings, commentary. It writes no number that
reaches the score.

This is also where the premarket/market-open distinction is enforced. In the
premarket stage no contract is selected at all, because option quotes before the
options market opens are stale or absent and a structure chosen against them is
chosen against a fiction. The report says so instead of quoting one.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.domain.market import PriceHistory, Quote
from app.logging_config import get_logger
from app.multiagent.analysis.alignment import build_alignment_snapshot
from app.multiagent.analysis.catalyst import validate_catalyst
from app.multiagent.analysis.contract_quality import build_contract_quality, build_risk_reward
from app.multiagent.analysis.flow import build_flow_snapshot
from app.multiagent.analysis.technical import (
    IndicatorContext,
    run_indicators,
    swing_levels,
    trend_bias,
)
from app.multiagent.config import MethodologyConfig
from app.multiagent.evidence.collector import ProviderCallRecorder, sector_proxy_for
from app.multiagent.models.brief import IndexContext
from app.multiagent.models.candidates import ResearchCandidate
from app.multiagent.models.contracts import ProposedStructure
from app.multiagent.models.enums import (
    AgentName,
    PipelineStage,
    RunStatus,
    ValidationVerdict,
)
from app.multiagent.models.evidence import EvidenceLedger
from app.multiagent.models.runs import AgentRunRecord
from app.multiagent.models.validation import ValidationReport
from app.multiagent.selection import propose_structures
from app.providers import registry

log = get_logger(__name__)


class SymbolData:
    """Freshly-retrieved, per-candidate market state."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.quote: Quote | None = None
        self.history: PriceHistory | None = None
        self.fundamentals: Any = None
        self.chain: Any = None
        self.iv_context: Any = None
        self.flow: Any = None
        self.sector_history: PriceHistory | None = None
        self.errors: dict[str, str] = {}
        self.cross_check_price: float | None = None
        self.cross_check_source: str | None = None

    @property
    def price(self) -> float | None:
        return self.quote.price if self.quote else None

    def disagreement_pct(self) -> float | None:
        """Gap between two independent underlying prices, or None.

        None when only one source answered. Reporting 0.0 there would claim the
        sources agreed when only one of them spoke.
        """
        if self.price is None or self.cross_check_price is None or self.price == 0:
            return None
        return round(abs(self.cross_check_price - self.price) / self.price * 100.0, 4)


async def fetch_symbol_data(
    symbol: str,
    *,
    recorder: ProviderCallRecorder,
    stage: PipelineStage,
    sector_hint: str | None = None,
) -> SymbolData:
    """Retrieve everything one candidate needs, concurrently and defensively."""
    data = SymbolData(symbol)
    md = registry.market_data_provider()
    fnd = registry.fundamentals_provider()
    chain_p = registry.options_chain_provider()
    flow_p = registry.options_flow_provider()

    tasks: dict[str, Any] = {
        "quote": recorder.call(md.name, "get_quote", md.get_quote(symbol), symbol=symbol),
        "history": recorder.call(
            md.name, "get_price_history", md.get_price_history(symbol, lookback_days=180), symbol=symbol
        ),
        "fundamentals": recorder.call(
            fnd.name, "get_fundamentals", fnd.get_fundamentals(symbol), symbol=symbol
        ),
        "iv": recorder.call(
            chain_p.name, "get_iv_context", chain_p.get_iv_context(symbol), symbol=symbol
        ),
        # The capability takes a single symbol and a count cap, not a time
        # window; the lookback is applied when the snapshot is built, from each
        # print's own timestamp. Filtering by count here and by time there keeps
        # the window definition in one place.
        "flow": recorder.call(
            flow_p.name,
            "get_flow_alerts",
            flow_p.get_flow_alerts(symbol, unusual_only=True, limit=200),
            symbol=symbol,
            default=None,
        ),
    }
    # The chain is retrieved ONLY at the market-open stage. See the module
    # docstring: a premarket chain is stale by construction.
    if stage in (PipelineStage.MARKET_OPEN, PipelineStage.FULL):
        tasks["chain"] = recorder.call(
            chain_p.name,
            "get_option_chain",
            chain_p.get_option_chain(symbol, expirations=8),
            symbol=symbol,
        )

    keys = list(tasks)
    results = await asyncio.gather(*(tasks[k] for k in keys))
    resolved = dict(zip(keys, results, strict=True))

    data.quote = resolved.get("quote")
    data.history = resolved.get("history")
    data.fundamentals = resolved.get("fundamentals")
    data.iv_context = resolved.get("iv")
    data.flow = resolved.get("flow")
    data.chain = resolved.get("chain")

    # Cross-check the underlying price against a second, independent source:
    # the option chain carries its own underlying mark. Where they disagree
    # materially the data-quality rule and a hard rule both see it.
    if data.chain is not None and getattr(data.chain, "underlying_price", None):
        data.cross_check_price = data.chain.underlying_price
        data.cross_check_source = getattr(data.chain, "source", "option_chain")

    sector = sector_hint or getattr(data.fundamentals, "sector", None)
    proxy = sector_proxy_for(sector)
    if proxy:
        data.sector_history = await recorder.call(
            md.name,
            "get_price_history",
            md.get_price_history(proxy, lookback_days=90),
            symbol=proxy,
        )

    data.errors = recorder.errors()
    return data


def build_measured_report(
    candidate: ResearchCandidate,
    data: SymbolData,
    ledger: EvidenceLedger,
    cfg: MethodologyConfig,
    indices: dict[str, IndexContext],
    *,
    now: datetime,
    stage: PipelineStage,
    scheduled_macro: list[tuple[str, datetime]] | None = None,
) -> ValidationReport:
    """Every measurement, before the agent sees anything."""
    report = ValidationReport(
        candidate_id=candidate.candidate_id,
        run_id=candidate.run_id,
        ticker=candidate.ticker,
        validated_at=now,
        stage=stage.value,
    )
    tech_cfg = cfg.scoring.technical_setup
    align_cfg = cfg.scoring.market_alignment
    flow_cfg = cfg.scoring.options_flow
    cat_cfg = cfg.scoring.catalyst_strength

    # --- 1. price / technical structure --------------------------------
    candles = list(data.history.candles) if data.history else []
    ictx = IndicatorContext(
        symbol=candidate.ticker,
        now=now,
        direction=candidate.direction,
        quote=data.quote,
        history=data.history,
        candles=candles,
        params={
            "atr_period": tech_cfg.atr_period,
            "momentum_lookback_days": tech_cfg.momentum_lookback_days,
            "trend_lookback": align_cfg.relative_strength_lookback_days,
        },
    )
    measurements, notes = run_indicators(ictx)
    supports, resistances = swing_levels(candles)
    report.technical = _technical_snapshot(
        candidate, data, measurements, notes, supports, resistances, align_cfg, now
    )

    # --- 2. market alignment -------------------------------------------
    sector = getattr(data.fundamentals, "sector", None)
    report.alignment = build_alignment_snapshot(
        candidate.ticker,
        candidate.direction,
        now=now,
        symbol_history=data.history,
        indices=indices,
        sector=sector,
        sector_proxy=sector_proxy_for(sector),
        sector_history=data.sector_history,
        lookback_days=align_cfg.relative_strength_lookback_days,
        flat_threshold_pct=align_cfg.trend_flat_threshold_pct,
    )

    # --- 3. catalyst ----------------------------------------------------
    report.catalyst = validate_catalyst(
        candidate,
        ledger,
        now=now,
        history=data.history,
        current_price=data.price,
        max_news_age_days=cat_cfg.max_news_age_days,
        priced_in_move_pct=cat_cfg.priced_in_move_pct,
        scheduled_macro=scheduled_macro,
    )

    # --- 4. options flow ------------------------------------------------
    report.flow = build_flow_snapshot(
        candidate.ticker,
        data.flow,
        candidate.direction,
        now=now,
        lookback_hours=flow_cfg.lookback_hours,
        min_premium_usd=flow_cfg.min_premium_usd,
        net_premium_ratio_strong=flow_cfg.net_premium_ratio_strong,
        ask_side_share_strong=flow_cfg.ask_side_share_strong,
        size_over_oi_ratio=flow_cfg.size_over_oi_ratio,
        provider_error=data.errors.get("flow"),
    )

    # --- 5 & 6. contracts, quality, risk/reward -------------------------
    if stage is PipelineStage.PREMARKET:
        report.data_gaps.append(
            "premarket stage: no option chain retrieved and no contract selected. Option quotes "
            "before the options market opens are stale or absent, and a structure chosen against "
            "them would be chosen against a fiction."
        )
    elif data.chain is None:
        report.data_gaps.append("no option chain retrieved — contract selection could not run")
    else:
        structures, sel_notes = propose_structures(
            data.chain,
            candidate.strategy_type,
            candidate.direction,
            cfg.contracts,
            candidate_id=candidate.candidate_id,
            run_id=candidate.run_id,
            now=now,
            max_risk_usd=cfg.hard_rules.max_defined_risk_usd,
            expected_move_pct=candidate.expected_move.magnitude_pct,
            allowed_strategies=set(cfg.strategies.allowed),
        )
        report.structures = structures
        report.data_gaps.extend(sel_notes)
        if structures:
            chosen = structures[0]
            report.selected_structure_id = chosen.structure_id
            report.contract_quality = build_contract_quality(chosen, data.iv_context, now=now)
            report.risk_reward = build_risk_reward(
                chosen,
                candidate,
                now=now,
                current_price=data.price,
                max_risk_usd=cfg.hard_rules.max_defined_risk_usd,
            )

    report.provider_errors = dict(data.errors)
    for key, err in data.errors.items():
        report.data_gaps.append(f"{key}: {err}")
    return report


def _technical_snapshot(candidate, data, measurements, notes, supports, resistances, align_cfg, now):
    from app.multiagent.models.validation import TechnicalSnapshot

    snap = TechnicalSnapshot(
        symbol=candidate.ticker,
        as_of=(data.quote.as_of if data.quote and data.quote.as_of else now),
        source=(data.quote.source if data.quote else "unknown"),
        price=data.price,
        prev_close=(data.quote.prev_close if data.quote else None),
        measurements=measurements,
        support_levels=supports[-5:],
        resistance_levels=resistances[:5],
        notes=notes,
        bars_available=len(data.history.candles) if data.history else 0,
    )
    snap.trend_bias = trend_bias(measurements, align_cfg.trend_flat_threshold_pct)
    if snap.bars_available == 0:
        snap.notes.append("no price history retrieved — every technical rule abstains")
    return snap


def _category_verdicts(report: ValidationReport, candidate: ResearchCandidate) -> dict[str, str]:
    """Per-category verdicts, derived mechanically from the measurements.

    Each is a tally of what was measured, not a judgement. They are handed to
    the agent as a starting point and shown in the report; none of them awards a
    point.
    """
    out: dict[str, str] = {}

    if report.catalyst:
        out["catalyst"] = report.catalyst.verdict.value
    if report.flow:
        out["flow"] = report.flow.verdict.value

    al = report.alignment
    if al:
        if al.fighting_the_tape:
            out["alignment"] = ValidationVerdict.CONTRADICTS.value
        elif al.aligned_with_spy is None:
            out["alignment"] = ValidationVerdict.INSUFFICIENT_DATA.value
        elif al.aligned_with_spy:
            out["alignment"] = ValidationVerdict.CONFIRMS.value
        else:
            out["alignment"] = ValidationVerdict.MIXED.value

    tech = report.technical
    if tech:
        # The trend confirms only when it runs the way the candidate needs it to.
        wanted = "bullish" if candidate.is_bullish() else "bearish"
        bias = tech.trend_bias.value
        if bias == "unknown":
            out["technical"] = ValidationVerdict.INSUFFICIENT_DATA.value
        elif bias == wanted:
            out["technical"] = ValidationVerdict.CONFIRMS.value
        elif bias == "neutral":
            out["technical"] = ValidationVerdict.MIXED.value
        else:
            out["technical"] = ValidationVerdict.CONTRADICTS.value

    cq = report.contract_quality
    if cq:
        out["contract_quality"] = (
            ValidationVerdict.INSUFFICIENT_DATA.value
            if cq.worst_spread_pct is None
            else ValidationVerdict.CONFIRMS.value
        )

    rr = report.risk_reward
    if rr:
        value = rr.reward_to_risk if rr.reward_to_risk is not None else rr.target_reward_to_risk
        if value is None:
            out["risk_reward"] = ValidationVerdict.INSUFFICIENT_DATA.value
        elif value >= 2.0:
            out["risk_reward"] = ValidationVerdict.CONFIRMS.value
        elif value >= 1.2:
            out["risk_reward"] = ValidationVerdict.MIXED.value
        else:
            out["risk_reward"] = ValidationVerdict.CONTRADICTS.value
    return out


def _findings(report: ValidationReport, candidate: ResearchCandidate) -> tuple[list[str], list[str]]:
    """Mechanical findings from the measurements, handed to the agent as a floor.

    The agent may add to these. It cannot remove them — a disconfirming
    measurement stays in the report whatever the agent concludes.
    """
    confirming: list[str] = []
    disconfirming: list[str] = []

    cv = report.catalyst
    if cv:
        if cv.verdict is ValidationVerdict.CONFIRMS:
            confirming.append(
                f"catalyst resolves to {len(cv.resolved_evidence_ids)} retrieved item(s), "
                f"quality {cv.evidence_quality.value}"
            )
        if cv.likely_priced_in:
            disconfirming.append(
                f"{candidate.ticker} has already moved {cv.move_since_catalyst_pct:+.1f}% since "
                "the catalyst published — the move may be behind us"
            )
        if cv.unresolved_refs:
            disconfirming.append(
                f"{len(cv.unresolved_refs)} cited evidence reference(s) did not resolve"
            )
        if cv.conflicting_events:
            disconfirming.append(
                "scheduled events land inside the hold: " + "; ".join(cv.conflicting_events[:3])
            )

    al = report.alignment
    if al:
        if al.fighting_the_tape:
            disconfirming.append(
                f"direction opposes both SPY ({al.spy_bias.value}) and QQQ ({al.qqq_bias.value})"
            )
        elif al.aligned_with_spy:
            confirming.append(f"aligned with SPY ({al.spy_bias.value})")

    fl = report.flow
    if fl:
        if fl.verdict is ValidationVerdict.CONTRADICTS:
            disconfirming.append(f"options flow argues the other way: {fl.interpretation}")
        elif fl.verdict is ValidationVerdict.CONFIRMS:
            confirming.append(fl.interpretation)
        elif fl.direction_ambiguous:
            disconfirming.append("options flow is directionally ambiguous — no confirmation available")

    tech = report.technical
    if tech:
        ext = tech.measurements.get("extension_atr")
        if ext.present and ext.require() > 2.5:
            disconfirming.append(
                f"price is {ext.require():.1f} ATR from its 20-bar mean — entry would be chasing"
            )
        if tech.trend_bias.value in ("bullish", "bearish"):
            confirming.append(f"measured 20-bar trend is {tech.trend_bias.value}")

    rr = report.risk_reward
    if rr:
        if rr.theta_burden is not None and rr.theta_burden > 0.4:
            disconfirming.append(
                f"decay consumes {rr.theta_burden:.0%} of the premium over the expected hold"
            )
        if rr.breakeven_move_pct is not None and rr.expected_move_pct:
            ratio = rr.breakeven_move_pct / rr.expected_move_pct
            if ratio > 1.0:
                disconfirming.append(
                    f"breakeven needs a {rr.breakeven_move_pct:.1f}% move against an IV-implied "
                    f"{rr.expected_move_pct:.1f}% — the market does not price this move as likely"
                )
            else:
                confirming.append(
                    f"breakeven ({rr.breakeven_move_pct:.1f}%) sits inside the IV-implied move "
                    f"({rr.expected_move_pct:.1f}%)"
                )

    cq = report.contract_quality
    if cq and cq.worst_spread_pct is not None and cq.worst_spread_pct > 0.10:
        disconfirming.append(f"widest leg spread is {cq.worst_spread_pct:.1%} of mid")

    return confirming, disconfirming


_PROMPT = """\
# Run {run_id} — adversarial validation of {ticker}

Current time (UTC): {now} | stage: {stage}

## The candidate you are testing

{ticker} {direction} via {strategy}
Thesis: {thesis}
Primary catalyst: {catalyst}
Invalidation as stated: {invalidation}
Expected hold: {hold}

## Measurements (computed by application code — do not restate or recompute)

{measurements}

## Selected structure

{structure}

## Mechanical findings

Confirming:
{confirming}

Disconfirming:
{disconfirming}

## Data gaps

{gaps}

## Task

Look for reasons this trade is WRONG. Return per-category and overall verdicts,
plus any confirming or disconfirming findings the mechanical pass missed. Do not
produce numbers — every figure above is already measured. If nothing contradicts
the idea, say so explicitly and say why.
"""


def _render_measurements(report: ValidationReport) -> str:
    lines: list[str] = []
    for name, snap in (
        ("technical", report.technical),
        ("alignment", report.alignment),
        ("flow", report.flow),
        ("contract_quality", report.contract_quality),
        ("risk_reward", report.risk_reward),
    ):
        if snap is None:
            lines.append(f"### {name}: not measured")
            continue
        lines.append(f"### {name}")
        for key, m in sorted(snap.measurements.measurements.items()):
            lines.append(f"- {key} = {m.export()}{(' ' + m.unit) if m.unit else ''} [{m.provenance.value}]")
    if report.catalyst:
        cv = report.catalyst
        lines.append("### catalyst")
        lines.append(f"- exists = {cv.exists}")
        lines.append(f"- resolved_evidence = {len(cv.resolved_evidence_ids)}")
        lines.append(f"- newest_evidence_age_days = {cv.newest_evidence_age_days}")
        lines.append(f"- likely_priced_in = {cv.likely_priced_in}")
        lines.append(f"- verdict = {cv.verdict.value}")
    return "\n".join(lines)


async def run_trade_validator(
    definition,
    runner,
    candidate: ResearchCandidate,
    report: ValidationReport,
    *,
    run_id: str,
    stage: PipelineStage,
    now: datetime | None = None,
) -> tuple[ValidationReport, AgentRunRecord]:
    """Attach the agent's adversarial reading to an already-measured report."""
    from app.multiagent.llm.runner import AgentInvocation

    when = now or datetime.now(UTC)
    record = AgentRunRecord(
        agent=AgentName.TRADE_VALIDATOR,
        run_id=run_id,
        stage=stage,
        started_at=when,
        runner=runner.runner_id,
        definition_path=str(definition.path),
    )

    confirming, disconfirming = _findings(report, candidate)
    verdicts = _category_verdicts(report, candidate)
    structure = report.selected_structure()

    prompt = _PROMPT.format(
        run_id=run_id,
        ticker=candidate.ticker,
        now=when.isoformat(),
        stage=stage.value,
        direction=candidate.direction.value,
        strategy=candidate.strategy_type.value,
        thesis=candidate.thesis,
        catalyst=candidate.primary_catalyst,
        invalidation=candidate.invalidation_thesis or "(none stated)",
        hold=candidate.expected_holding_period.value,
        measurements=_render_measurements(report),
        structure=structure.describe() if structure else "(no structure selected)",
        confirming="\n".join(f"- {c}" for c in confirming) or "- none",
        disconfirming="\n".join(f"- {d}" for d in disconfirming) or "- none",
        gaps="\n".join(f"- {g}" for g in report.data_gaps) or "- none",
    )
    record.record_prompt(prompt)

    result = await runner.run(
        AgentInvocation(
            definition=definition,
            user_prompt=prompt,
            output_schema=_validation_schema(),
            context={
                "category_verdicts": verdicts,
                "confirming": confirming,
                "disconfirming": disconfirming,
                "data_gaps": list(report.data_gaps),
                "now": when,
            },
        )
    )
    record.record_response(result.raw_text)
    record.errors.extend(result.errors)
    record.input_tokens = result.input_tokens
    record.output_tokens = result.output_tokens

    # The mechanical findings are the floor. The agent adds; it cannot remove.
    report.confirming_findings = list(confirming)
    report.disconfirming_findings = list(disconfirming)

    payload = result.data if isinstance(result.data, dict) else None
    if payload is None:
        report.overall_verdict = ValidationVerdict.INSUFFICIENT_DATA
        report.agent_commentary = "Validator agent returned no usable output; mechanical findings stand."
        record.finish(RunStatus.FAILED, datetime.now(UTC))
        return report, record

    try:
        report.overall_verdict = ValidationVerdict(str(payload.get("overall_verdict", "insufficient_data")))
    except ValueError:
        report.overall_verdict = ValidationVerdict.INSUFFICIENT_DATA
        record.validation_warnings.append(
            f"agent returned an unrecognised verdict {payload.get('overall_verdict')!r}"
        )

    for extra in payload.get("confirming_findings") or []:
        text = str(extra)
        if text not in report.confirming_findings:
            report.confirming_findings.append(text)
    for extra in payload.get("disconfirming_findings") or []:
        text = str(extra)
        if text not in report.disconfirming_findings:
            report.disconfirming_findings.append(text)
    for gap in payload.get("data_gaps") or []:
        text = str(gap)
        if text not in report.data_gaps:
            report.data_gaps.append(text)
    report.agent_commentary = str(payload.get("agent_commentary", ""))

    record.structured_output = {
        "overall_verdict": report.overall_verdict.value,
        "confirming": len(report.confirming_findings),
        "disconfirming": len(report.disconfirming_findings),
    }
    record.finish(RunStatus.COMPLETED, datetime.now(UTC))
    return report, record


def _validation_schema() -> dict[str, Any]:
    """Interpretation only — no numeric fields are accepted from the agent."""
    return {
        "type": "object",
        "properties": {
            "overall_verdict": {
                "type": "string",
                "enum": [v.value for v in ValidationVerdict],
            },
            "confirming_findings": {"type": "array", "items": {"type": "string"}},
            "disconfirming_findings": {"type": "array", "items": {"type": "string"}},
            "data_gaps": {"type": "array", "items": {"type": "string"}},
            "agent_commentary": {"type": "string"},
        },
        "required": ["overall_verdict", "agent_commentary"],
    }


def selected_structure_of(report: ValidationReport) -> ProposedStructure | None:
    return report.selected_structure()
