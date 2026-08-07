"""Observability records: what each agent was asked, what it returned, what broke.

The spec's requirement — *"I should be able to understand why a recommendation
was produced"* — needs the agent side of the story as much as the data side.
`AgentRunRecord` captures the whole invocation: the prompt actually sent, the
raw response, the parsed output, which providers were touched, what was dropped.

Secrets never reach these records. `redact()` runs over any string field that
leaves the process, and `app.multiagent.llm.runner` never places a credential
into a prompt in the first place — providers are called by Python, and agents
see retrieved data, not API keys.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.multiagent.models.enums import AgentName, DataQualityFlag, PipelineStage, RunStatus

# Patterns that must never appear in a stored artifact. Belt and braces on top
# of the rule that credentials are simply not put into prompts: CLAUDE.md §4
# treats a key written anywhere but a secret store as already compromised.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)\b(api[_-]?key|auth[_-]?token|password|secret|bearer)\b\s*[:=]\s*\S+"),
)


def redact(text: str) -> str:
    """Blank anything that pattern-matches a credential."""
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


class ProviderRequestRecord(BaseModel):
    """One outbound data call. No URLs with query strings — they carry keys."""

    provider: str
    capability: str
    symbol: str | None = None
    started_at: datetime
    duration_ms: float | None = None
    ok: bool = True
    error: str | None = None
    result_count: int | None = None
    cache_hit: bool = False


class DataQualityRecord(BaseModel):
    flag: DataQualityFlag
    subject: str            # symbol, evidence id, field name
    detail: str = ""
    observed_at: datetime


class AgentRunRecord(BaseModel):
    agent: AgentName
    run_id: str
    stage: PipelineStage

    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: float | None = None
    status: RunStatus = RunStatus.RUNNING

    # Which implementation answered: "deterministic" or "anthropic:<model>".
    runner: str = "unknown"
    definition_path: str | None = None

    # Truncated for storage; full text stays in the process.
    prompt_excerpt: str = ""
    raw_response_excerpt: str = ""
    structured_output: dict[str, Any] | None = None

    tools_used: list[str] = Field(default_factory=list)
    providers_queried: list[str] = Field(default_factory=list)
    provider_requests: list[ProviderRequestRecord] = Field(default_factory=list)

    errors: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    validation_warnings: list[str] = Field(default_factory=list)
    dropped_claims: list[str] = Field(default_factory=list)

    input_tokens: int | None = None
    output_tokens: int | None = None

    def record_prompt(self, prompt: str, limit: int = 4000) -> None:
        self.prompt_excerpt = redact(prompt)[:limit]

    def record_response(self, response: str, limit: int = 8000) -> None:
        self.raw_response_excerpt = redact(response)[:limit]

    def finish(self, status: RunStatus, finished_at: datetime) -> None:
        self.status = status
        self.finished_at = finished_at
        self.duration_ms = round((finished_at - self.started_at).total_seconds() * 1000.0, 2)


class PipelineRun(BaseModel):
    """The run itself — the row every other record hangs off."""

    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    stage: PipelineStage
    status: RunStatus = RunStatus.RUNNING

    methodology_version: str = ""
    scoring_model_version: str = ""
    agent_runner: str = "unknown"

    trading_mode: str = "research"
    # Recorded on every run so the corpus can prove no run ever placed an order.
    execution_enabled: bool = False

    agent_runs: list[AgentRunRecord] = Field(default_factory=list)
    provider_requests: list[ProviderRequestRecord] = Field(default_factory=list)
    data_quality: list[DataQualityRecord] = Field(default_factory=list)

    notes: list[str] = Field(default_factory=list)
