"""Agent 2 wrapper: hypotheses in, validated candidates out.

Enforced here, not requested of the agent:

* **the candidate cap** — trimmed to `run.max_candidates` after parsing,
* **the strategy allow-list** — anything outside it is dropped,
* **the ticker whitelist** — a candidate on a symbol with no retrieved data
  cannot be validated, so it is dropped rather than carried forward to fail
  later with a confusing diagnosis,
* **evidence binding** — a candidate whose primary catalyst cites nothing
  resolvable is dropped,
* **the reference price** — written from the real quote, never from the agent.

Every drop is recorded with its reason. The report shows them, because "the
agent proposed six ideas and four were unusable" is information about the agent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.domain.market import Quote
from app.logging_config import get_logger
from app.multiagent.agents.binding import (
    BindingResult,
    bind_claims,
    restrict_to_known_symbols,
)
from app.multiagent.config import MethodologyConfig
from app.multiagent.models.brief import MarketBrief
from app.multiagent.models.candidates import ResearchCandidate
from app.multiagent.models.enums import (
    AgentName,
    BiasDirection,
    PipelineStage,
    RunStatus,
)
from app.multiagent.models.evidence import EvidenceLedger
from app.multiagent.models.runs import AgentRunRecord

log = get_logger(__name__)

_PROMPT = """\
# Run {run_id} — options opportunity generation

Current time (UTC): {now}
Stage: {stage}

## Market brief from Agent 1

Regime: {regime} | volatility: {vol_regime}
SPY bias: {spy_bias} | QQQ bias: {qqq_bias}

{summary}

### Company catalysts (evidence-bound)

{catalysts}

### Risk events inside the horizon

{risks}

### Data gaps Agent 1 reported

{gaps}

## Measured 20-day trend per ticker (application code — do not restate)

{trends}

## Constraints

- At most {max_candidates} candidates.
- Allowed strategies: {allowed}.
- Only these tickers have retrieved data and can be validated: {universe}
- Cite evidence ids from the catalysts above. A candidate whose primary catalyst
  cites nothing resolvable is dropped and its slot is wasted.
- Prefer returning fewer candidates, or none, over forcing a weak setup.

## Task

Return a JSON array of ResearchCandidate objects. State an invalidation for each.
Do not state any price, strike, expiration, IV, volume or Greek — you have no
live option data and contract selection is Agent 3's job.
"""


def _render_catalysts(brief: MarketBrief) -> str:
    if not brief.company_catalysts:
        return "(none identified)"
    lines = []
    for c in brief.company_catalysts:
        when = c.published_at.isoformat() if c.published_at else "published_at=NA_no_data"
        lines.append(
            f"- [{', '.join(c.evidence_refs)}] {c.ticker} {c.catalyst_type.value} "
            f"dir={c.expected_direction.value} importance={c.importance.value} "
            f"horizon={c.expected_time_horizon.value} quality={c.evidence_quality.value} "
            f"{when} :: {c.headline}"
        )
    return "\n".join(lines)


def _render_trends(trends: dict[str, BiasDirection]) -> str:
    if not trends:
        return "(no trend measurements available)"
    return "\n".join(f"- {sym}: {bias.value}" for sym, bias in sorted(trends.items()))


def build_prompt(
    brief: MarketBrief,
    cfg: MethodologyConfig,
    *,
    run_id: str,
    stage: PipelineStage,
    now: datetime,
    trends: dict[str, BiasDirection],
    universe: list[str],
) -> str:
    return _PROMPT.format(
        run_id=run_id,
        now=now.isoformat(),
        stage=stage.value,
        regime=brief.market_regime.value,
        vol_regime=brief.volatility_regime.value,
        spy_bias=brief.spy_bias.value,
        qqq_bias=brief.qqq_bias.value,
        summary=brief.summary or "(no summary)",
        catalysts=_render_catalysts(brief),
        risks="\n".join(f"- {r.name}: {r.description}" for r in brief.risk_events) or "- none",
        gaps="\n".join(f"- {g}" for g in brief.data_gaps) or "- none",
        trends=_render_trends(trends),
        max_candidates=cfg.run.max_candidates,
        allowed=", ".join(cfg.strategies.allowed),
        universe=", ".join(sorted(universe)),
    )


async def run_opportunity_generator(
    definition,
    runner,
    brief: MarketBrief,
    ledger: EvidenceLedger,
    cfg: MethodologyConfig,
    *,
    run_id: str,
    stage: PipelineStage,
    trends: dict[str, BiasDirection],
    quotes: dict[str, Quote],
    now: datetime | None = None,
) -> tuple[list[ResearchCandidate], AgentRunRecord, BindingResult]:
    from app.multiagent.llm.runner import AgentInvocation

    when = now or datetime.now(UTC)
    record = AgentRunRecord(
        agent=AgentName.OPPORTUNITY_GENERATOR,
        run_id=run_id,
        stage=stage,
        started_at=when,
        runner=runner.runner_id,
        definition_path=str(definition.path),
    )
    binding = BindingResult()

    universe = sorted(set(quotes) | ledger.symbols())
    prompt = build_prompt(
        brief, cfg, run_id=run_id, stage=stage, now=when, trends=trends, universe=universe
    )
    record.record_prompt(prompt)

    result = await runner.run(
        AgentInvocation(
            definition=definition,
            user_prompt=prompt,
            output_schema=_candidate_list_schema(),
            context={
                "brief": brief.model_dump(mode="json"),
                "ledger": ledger,
                "trends": trends,
                "max_candidates": cfg.run.max_candidates,
                "allowed_strategies": list(cfg.strategies.allowed),
                "now": when,
            },
        )
    )
    record.record_response(result.raw_text)
    record.errors.extend(result.errors)
    record.input_tokens = result.input_tokens
    record.output_tokens = result.output_tokens

    raw = result.data
    if raw is None:
        record.finish(RunStatus.FAILED, datetime.now(UTC))
        return [], record, binding
    # A model given an object schema may wrap the array; accept either shape.
    if isinstance(raw, dict):
        raw = raw.get("candidates") or raw.get("items") or []
    if not isinstance(raw, list):
        record.errors.append(f"expected a list of candidates, got {type(raw).__name__}")
        record.finish(RunStatus.FAILED, datetime.now(UTC))
        return [], record, binding

    items = [dict(c) for c in raw if isinstance(c, dict)]

    # 1. Tickers we actually have data for.
    items = restrict_to_known_symbols(
        items, set(universe), label="candidate", now=when, result=binding
    )
    # 2. Primary catalyst must resolve.
    items = bind_claims(
        items,
        ledger,
        ref_field="primary_catalyst_refs",
        label="candidate primary catalyst",
        now=when,
        require_refs=True,
        result=binding,
    )
    # 3. General refs are stripped but do not disqualify.
    items = bind_claims(
        items, ledger, label="candidate", now=when, require_refs=False, result=binding
    )
    # 4. Supporting catalysts are bound individually.
    for item in items:
        sup = item.get("supporting_catalysts") or []
        if isinstance(sup, list):
            item["supporting_catalysts"] = bind_claims(
                [dict(s) for s in sup if isinstance(s, dict)],
                ledger,
                label="supporting catalyst",
                now=when,
                require_refs=True,
                result=binding,
            )

    allowed = set(cfg.strategies.allowed)
    candidates: list[ResearchCandidate] = []
    seen: set[tuple[str, str]] = set()

    for item in items:
        strategy = str(item.get("strategy_type", ""))
        if strategy not in allowed:
            binding.dropped.append(
                f"candidate {item.get('ticker', '?')}: strategy {strategy!r} is not in the allow-list"
            )
            continue
        item.update({"run_id": run_id, "generated_at": when})
        try:
            candidate = ResearchCandidate.model_validate(item)
        except Exception as exc:  # noqa: BLE001
            binding.dropped.append(
                f"candidate {item.get('ticker', '?')}: schema validation failed ({str(exc)[:160]})"
            )
            continue

        key = (candidate.ticker, candidate.direction.value)
        if key in seen:
            binding.dropped.append(
                f"candidate {candidate.ticker} {candidate.direction.value}: duplicate of an "
                "earlier candidate"
            )
            continue
        seen.add(key)

        # The reference price is measured, never taken from the agent.
        quote = quotes.get(candidate.ticker)
        if quote is not None:
            candidate.underlying_reference_price = quote.price
            candidate.underlying_reference_as_of = quote.as_of
        candidate.dropped_claims = []
        candidates.append(candidate)

    if len(candidates) > cfg.run.max_candidates:
        binding.dropped.append(
            f"{len(candidates) - cfg.run.max_candidates} candidate(s) beyond the "
            f"{cfg.run.max_candidates} cap were discarded"
        )
        candidates = candidates[: cfg.run.max_candidates]

    record.dropped_claims = list(binding.dropped)
    record.validation_warnings = [binding.summary()] if not binding.clean else []
    record.structured_output = {
        "candidates": len(candidates),
        "tickers": [c.ticker for c in candidates],
    }
    record.finish(RunStatus.COMPLETED, datetime.now(UTC))
    log.info(
        "multiagent_candidates_generated",
        run_id=run_id,
        candidates=len(candidates),
        dropped=len(binding.dropped),
    )
    return candidates, record, binding


def _candidate_list_schema() -> dict[str, Any]:
    item = ResearchCandidate.model_json_schema()
    props = item.get("properties", {})
    for key in (
        "candidate_id",
        "run_id",
        "generated_at",
        "underlying_reference_price",
        "underlying_reference_as_of",
        "dropped_claims",
    ):
        props.pop(key, None)
    item["required"] = [r for r in item.get("required", []) if r in props]
    return {
        "type": "object",
        "properties": {"candidates": {"type": "array", "items": item}},
        "required": ["candidates"],
    }


def candidate_schema_json() -> str:
    return json.dumps(_candidate_list_schema(), indent=2, default=str)
