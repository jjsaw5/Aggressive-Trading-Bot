"""Agent 1 wrapper: build the prompt, run the agent, bind the result.

The wrapper owns three things the agent is not trusted with:

1. **The measured fields.** `spy`, `qqq`, `iwm` and the VIX level are written
   here from provider data *after* the agent returns, overwriting anything it
   said. An agent cannot restate a price, correctly or otherwise.
2. **Evidence binding.** Every catalyst, news item, macro event and risk event
   is filtered through `bind_claims`.
3. **The run record.** Prompt excerpt, response excerpt, timings, dropped claims
   and provider errors, so the brief can be explained later.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.logging_config import get_logger
from app.multiagent.agents.binding import BindingResult, bind_claims
from app.multiagent.config import MethodologyConfig
from app.multiagent.evidence.collector import MarketEvidence
from app.multiagent.llm.definitions import AgentDefinition
from app.multiagent.llm.runner import AgentInvocation, AgentRunner
from app.multiagent.models.brief import MarketBrief, SourceReference
from app.multiagent.models.enums import (
    AgentName,
    BiasDirection,
    PipelineStage,
    RunStatus,
)
from app.multiagent.models.runs import AgentRunRecord

log = get_logger(__name__)

_PROMPT = """\
# Run {run_id} — market intelligence

Current time (UTC): {now}
Stage: {stage}

## Measured index state (written by application code — do not restate or adjust)

{indices}

Volatility regime (from SPY IV rank): {vol_regime}
VIX: {vix}

## Evidence ledger

You may cite ONLY the ids below. A claim citing anything else is dropped by the
calling application and recorded against this run.

{ledger}

## Provider gaps

{gaps}

## Task

Produce a MarketBrief. Classify every catalyst by scope, expected direction,
importance, time horizon, scheduled-versus-unscheduled and evidence quality.
Put anything you wanted and could not get into `data_gaps`.
"""


def _render_indices(ev: MarketEvidence) -> str:
    if not ev.indices:
        return "(no index data retrieved)"
    lines = []
    for sym, ctx in ev.indices.items():
        price = f"{ctx.price:.2f}" if ctx.price is not None else "NA_no_data"
        chg = f"{ctx.change_pct:+.2f}%" if ctx.change_pct is not None else "NA_no_data"
        trail = (
            f"{ctx.trailing_20d_return_pct:+.2f}%"
            if ctx.trailing_20d_return_pct is not None
            else "NA_no_data"
        )
        lines.append(
            f"- {sym}: price={price} change={chg} 20d_return={trail} "
            f"above_20sma={ctx.above_20d_sma} above_50sma={ctx.above_50d_sma} "
            f"measured_bias={ctx.bias.value}"
        )
    return "\n".join(lines)


def build_prompt(ev: MarketEvidence, cfg: MethodologyConfig, *, run_id: str, stage: PipelineStage, now: datetime) -> str:
    vix = (
        f"level={ev.vix.level:.2f} ({ev.vix.source or 'unknown source'})"
        if ev.vix.level is not None
        else f"NA_no_data — {ev.vix.commentary or 'no VIX quote'}"
    )
    gaps = (
        "\n".join(f"- {k}: {v}" for k, v in ev.ledger.provider_errors.items())
        or "- none reported"
    )
    return _PROMPT.format(
        run_id=run_id,
        now=now.isoformat(),
        stage=stage.value,
        indices=_render_indices(ev),
        vol_regime=ev.volatility_regime.value,
        vix=vix,
        ledger=ev.ledger.render(limit=cfg.agents.max_evidence_items),
        gaps=gaps,
    )


async def run_market_intelligence(
    definition: AgentDefinition,
    runner: AgentRunner,
    ev: MarketEvidence,
    cfg: MethodologyConfig,
    *,
    run_id: str,
    stage: PipelineStage,
    now: datetime | None = None,
) -> tuple[MarketBrief, AgentRunRecord, BindingResult]:
    when = now or datetime.now(UTC)
    record = AgentRunRecord(
        agent=AgentName.MARKET_INTELLIGENCE,
        run_id=run_id,
        stage=stage,
        started_at=when,
        runner=runner.runner_id,
        definition_path=str(definition.path),
    )
    binding = BindingResult()

    prompt = build_prompt(ev, cfg, run_id=run_id, stage=stage, now=when)
    record.record_prompt(prompt)

    result = await runner.run(
        AgentInvocation(
            definition=definition,
            user_prompt=prompt,
            output_schema=_brief_schema(),
            context={
                "ledger": ev.ledger,
                "indices": ev.indices,
                "volatility_regime": ev.volatility_regime,
                "now": when,
            },
        )
    )
    record.record_response(result.raw_text)
    record.errors.extend(result.errors)
    record.input_tokens = result.input_tokens
    record.output_tokens = result.output_tokens
    record.providers_queried = sorted({r.provider for r in ev.requests})
    record.provider_requests = list(ev.requests)

    if result.data is None:
        record.finish(RunStatus.FAILED, datetime.now(UTC))
        brief = _empty_brief(run_id, when, cfg, stage, ev)
        brief.data_gaps.append("market intelligence agent returned no output")
        return brief, record, binding

    payload: dict[str, Any] = dict(result.data)

    # --- evidence binding ------------------------------------------------
    for key, label, require in (
        ("company_catalysts", "company catalyst", True),
        ("news_items", "news item", False),
        ("macro_events", "macro event", True),
        ("upcoming_scheduled_events", "scheduled event", True),
        ("risk_events", "risk event", True),
        ("sector_observations", "sector observation", False),
    ):
        items = payload.get(key) or []
        if isinstance(items, list):
            payload[key] = bind_claims(
                [dict(i) for i in items if isinstance(i, dict)],
                ev.ledger,
                label=label,
                now=when,
                require_refs=require,
                result=binding,
            )

    # `news_items` cite a single id in `evidence_id`, not a list. Bind those too.
    payload["news_items"] = [
        n for n in (payload.get("news_items") or []) if str(n.get("evidence_id", "")) in ev.ledger
    ]

    payload.update({"run_id": run_id, "generated_at": when, "stage": stage.value})
    payload["methodology_version"] = cfg.version

    try:
        brief = MarketBrief.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - a malformed brief must not kill the run
        record.errors.append(f"brief failed schema validation: {str(exc)[:400]}")
        record.finish(RunStatus.FAILED, datetime.now(UTC))
        brief = _empty_brief(run_id, when, cfg, stage, ev)
        brief.data_gaps.append("agent output did not satisfy the MarketBrief schema")
        return brief, record, binding

    # --- measured fields are authoritative, always -----------------------
    _apply_measurements(brief, ev)
    brief.dropped_claims = list(binding.dropped)
    brief.data_gaps.extend(f"{k}: {v}" for k, v in ev.ledger.provider_errors.items())
    brief.source_references = _source_refs(brief, ev)

    record.dropped_claims = list(binding.dropped)
    record.validation_warnings = ([binding.summary()] if not binding.clean else [])
    record.missing_data = list(brief.data_gaps)
    record.structured_output = {
        "catalysts": len(brief.company_catalysts),
        "news_items": len(brief.news_items),
        "macro_events": len(brief.macro_events),
        "risk_events": len(brief.risk_events),
        "market_regime": brief.market_regime.value,
    }
    record.finish(RunStatus.COMPLETED, datetime.now(UTC))
    log.info(
        "multiagent_brief_built",
        run_id=run_id,
        catalysts=len(brief.company_catalysts),
        dropped=len(binding.dropped),
        regime=brief.market_regime.value,
    )
    return brief, record, binding


def _apply_measurements(brief: MarketBrief, ev: MarketEvidence) -> None:
    """Overwrite index/VIX fields with measured values. Non-negotiable."""
    brief.spy = ev.indices.get("SPY")
    brief.qqq = ev.indices.get("QQQ")
    brief.iwm = ev.indices.get("IWM")
    brief.spy_bias = brief.spy.bias if brief.spy else BiasDirection.UNKNOWN
    brief.qqq_bias = brief.qqq.bias if brief.qqq else BiasDirection.UNKNOWN
    brief.vix = ev.vix
    brief.volatility_regime = ev.volatility_regime


def _source_refs(brief: MarketBrief, ev: MarketEvidence) -> list[SourceReference]:
    ids: dict[str, None] = {}
    for c in brief.company_catalysts:
        for r in c.evidence_refs:
            ids.setdefault(r, None)
    for n in brief.news_items:
        ids.setdefault(n.evidence_id, None)
    out: list[SourceReference] = []
    for ref in ids:
        item = ev.ledger.get(ref)
        if item is None:
            continue
        out.append(
            SourceReference(
                evidence_id=item.id,
                source=item.source,
                url=item.url,
                headline=item.headline,
                published_at=item.published_at,
                retrieved_at=item.retrieved_at,
            )
        )
    return out


def _empty_brief(
    run_id: str, when: datetime, cfg: MethodologyConfig, stage: PipelineStage, ev: MarketEvidence
) -> MarketBrief:
    """A brief with measured context but no interpretation.

    Returned when the agent fails. It still carries real index data, so the
    pipeline degrades to "here is the tape, no catalysts identified" rather than
    to nothing — and the gap says which happened.
    """
    brief = MarketBrief(
        run_id=run_id,
        generated_at=when,
        methodology_version=cfg.version,
        stage=stage.value,
        summary="Agent produced no usable brief. Measured index context is shown; no catalysts identified.",
    )
    _apply_measurements(brief, ev)
    brief.data_gaps.extend(f"{k}: {v}" for k, v in ev.ledger.provider_errors.items())
    return brief


def _brief_schema() -> dict[str, Any]:
    """JSON Schema handed to a model-backed runner.

    Derived from the Pydantic model so the schema and the parser cannot drift.
    `run_id`, `generated_at` and the measured fields are stripped: the wrapper
    writes them, and asking a model to supply a value that will be overwritten
    invites it to believe the value matters.
    """
    schema = MarketBrief.model_json_schema()
    props = schema.get("properties", {})
    for key in (
        "run_id",
        "generated_at",
        "methodology_version",
        "stage",
        "spy",
        "qqq",
        "iwm",
        "vix",
        "spy_bias",
        "qqq_bias",
        "volatility_regime",
        "source_references",
        "dropped_claims",
    ):
        props.pop(key, None)
    schema["required"] = [r for r in schema.get("required", []) if r in props]
    return schema


def brief_schema_json() -> str:
    return json.dumps(_brief_schema(), indent=2, default=str)
