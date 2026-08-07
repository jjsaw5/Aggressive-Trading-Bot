"""Agent execution: role definitions plus interchangeable runners."""

from __future__ import annotations

from app.multiagent.llm.definitions import (
    AgentDefinition,
    AgentDefinitionError,
    load_agent,
    load_all,
    load_definition,
)
from app.multiagent.llm.deterministic import DeterministicAgentRunner
from app.multiagent.llm.runner import (
    AgentInvocation,
    AgentResult,
    AgentRunner,
    AgentRunnerError,
)

__all__ = [
    "AgentDefinition",
    "AgentDefinitionError",
    "AgentInvocation",
    "AgentResult",
    "AgentRunner",
    "AgentRunnerError",
    "DeterministicAgentRunner",
    "build_runner",
    "load_agent",
    "load_all",
    "load_definition",
]


def build_runner(name: str = "deterministic", **kwargs) -> AgentRunner:
    """Resolve a runner by name.

    An unknown name raises rather than defaulting. Silently falling back to the
    deterministic runner when someone asked for a model would produce a corpus
    whose author is not what its records say.
    """
    key = (name or "deterministic").strip().lower()
    if key == "deterministic":
        return DeterministicAgentRunner()
    if key in {"anthropic", "claude"}:
        from app.multiagent.llm.anthropic_runner import AnthropicAgentRunner

        return AnthropicAgentRunner(**kwargs)
    raise AgentRunnerError(
        f"unknown agent runner {name!r}. Available: 'deterministic', 'anthropic'."
    )
