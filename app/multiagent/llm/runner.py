"""The agent-runner interface.

Every agent invocation goes through `AgentRunner.run`, which takes a role
definition plus a fully-built prompt and returns raw structured data. Parsing
that data into a Pydantic model, and binding its claims to the evidence ledger,
happens in `app.multiagent.agents.*` — deliberately outside the runner, so the
same validation applies no matter which runner answered.

Two implementations ship:

* `DeterministicAgentRunner` — no credentials required. Derives its output from
  the evidence the collector already retrieved, using explicit heuristics. It is
  **not** a mock returning canned market data; it never invents a headline, a
  price or a date, and every claim it makes cites a real ledger id. What it
  lacks is judgement, and the reports say so.
* `AnthropicAgentRunner` — calls the Claude API with the role definition as the
  system prompt and a JSON schema for the output.

`runner_id` is stamped on every `AgentRunRecord` and every persisted run, so a
stored recommendation always states which one produced it. That matters: a
deterministic-runner brief and a model-authored brief are different artifacts
and must never be compared as though they were the same.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from app.multiagent.llm.definitions import AgentDefinition


class AgentRunnerError(RuntimeError):
    pass


@dataclass
class AgentInvocation:
    """Everything an agent needs for one call."""

    definition: AgentDefinition
    user_prompt: str
    # JSON Schema the response must satisfy. Runners that can enforce it do.
    output_schema: dict[str, Any] | None = None
    # Structured inputs, available to a runner that reasons over objects rather
    # than text (the deterministic one does).
    context: dict[str, Any] = field(default_factory=dict)
    max_tokens: int = 8000
    temperature: float = 0.0


@dataclass
class AgentResult:
    """Raw agent output, before evidence binding."""

    data: Any
    raw_text: str = ""
    runner_id: str = "unknown"
    input_tokens: int | None = None
    output_tokens: int | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class AgentRunner(abc.ABC):
    """Resolve an invocation to structured data."""

    #: Stamped onto run records. Must identify the implementation AND, for a
    #: model-backed runner, the model — two different models are two different
    #: authors of the corpus.
    runner_id: str = "unknown"

    @abc.abstractmethod
    async def run(self, invocation: AgentInvocation) -> AgentResult:
        ...

    def describe(self) -> str:
        return self.runner_id
