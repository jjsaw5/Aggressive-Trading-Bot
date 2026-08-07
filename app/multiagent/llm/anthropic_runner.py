"""Claude-backed agent runner.

The `anthropic` SDK is an **optional** dependency (`pip install -e ".[llm]"`) and
is imported lazily, so the default install and the whole test suite run without
it. A missing SDK or a missing key is a loud, specific error at construction —
never a silent fallback to the deterministic runner, because a corpus that
quietly switched authors mid-run would be worthless.

Output is forced through a tool-call schema rather than "please reply with
JSON". Prose wrapped around a JSON blob is the single most common failure mode
of structured LLM output, and a tool schema removes it: the model either emits a
conforming object or the call fails visibly.

The system prompt is the markdown body of `.claude/agents/<name>.md` — the same
text Claude Code uses for the subagent. The anti-hallucination rules therefore
cannot drift between the two invocation paths.
"""

from __future__ import annotations

import json
from typing import Any

from app.logging_config import get_logger
from app.multiagent.llm.runner import AgentInvocation, AgentResult, AgentRunner, AgentRunnerError

log = get_logger(__name__)

_TOOL_NAME = "emit_structured_output"

# A blunt instruction appended to every system prompt. The role definitions
# already carry their own anti-hallucination rules; this is the invariant that
# must hold no matter which definition is loaded.
_SYSTEM_SUFFIX = """

---
## Output contract (enforced by the calling application)

Return your answer by calling the `emit_structured_output` tool exactly once.
Do not write prose outside the tool call.

Non-negotiable, checked by application code after you respond:

* Every `evidence_refs` entry MUST be an id present in the evidence ledger you
  were given. Claims citing an unknown id are dropped and recorded against your
  run — they do not reach the report.
* Never invent a price, a date, a headline, a URL, a Greek, an IV or a volume.
* When something is unavailable, say so in the designated gaps field rather than
  estimating it. A stated gap is a useful output; an estimate presented as a
  measurement is a defect.
"""


class AnthropicAgentRunner(AgentRunner):
    """Runs an agent definition against the Claude API with schema-forced output."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "claude-sonnet-5",
        max_retries: int = 2,
        timeout_seconds: float = 120.0,
    ) -> None:
        try:
            import anthropic  # noqa: F401
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise AgentRunnerError(
                "the `anthropic` package is not installed. Install the optional extra "
                'with `pip install -e ".[llm]"`, or run with the deterministic runner '
                "(`--runner deterministic`, the default)."
            ) from exc

        from app.config import settings

        key = api_key or getattr(settings, "anthropic_api_key", None)
        if not key:
            raise AgentRunnerError(
                "ANTHROPIC_API_KEY is not set. Put it in .env (gitignored) or the "
                "deployment's secret manager — never in a commit, a log line or a "
                "prompt. Run with the deterministic runner to work without one."
            )

        import anthropic

        self._client = anthropic.AsyncAnthropic(
            api_key=key, max_retries=max_retries, timeout=timeout_seconds
        )
        self._model = model
        self.runner_id = f"anthropic:{model}"

    async def run(self, invocation: AgentInvocation) -> AgentResult:
        schema = invocation.output_schema or {"type": "object"}
        system = invocation.definition.system_prompt + _SYSTEM_SUFFIX

        tools = [
            {
                "name": _TOOL_NAME,
                "description": (
                    "Emit the agent's structured result. Call exactly once. "
                    "This is the only accepted output channel."
                ),
                "input_schema": schema,
            }
        ]

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=invocation.max_tokens,
                temperature=invocation.temperature,
                system=system,
                tools=tools,
                tool_choice={"type": "tool", "name": _TOOL_NAME},
                messages=[{"role": "user", "content": invocation.user_prompt}],
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a run error, never swallowed
            log.warning(
                "multiagent_llm_call_failed",
                agent=invocation.definition.name,
                model=self._model,
                error=str(exc)[:300],
            )
            return AgentResult(
                data=None,
                runner_id=self.runner_id,
                errors=[f"anthropic call failed: {str(exc)[:300]}"],
            )

        payload: Any = None
        raw_parts: list[str] = []
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "tool_use" and getattr(block, "name", "") == _TOOL_NAME:
                payload = block.input
                raw_parts.append(json.dumps(block.input, default=str)[:8000])
            elif btype == "text":
                raw_parts.append(str(getattr(block, "text", "")))

        usage = getattr(response, "usage", None)
        result = AgentResult(
            data=payload,
            raw_text="\n".join(raw_parts),
            runner_id=self.runner_id,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )
        if payload is None:
            # Forced tool_choice makes this unlikely; if it happens the run is a
            # failure, not something to paper over with a partial parse.
            result.errors.append(
                f"model returned no {_TOOL_NAME} tool call "
                f"(stop_reason={getattr(response, 'stop_reason', 'unknown')})"
            )
        return result
