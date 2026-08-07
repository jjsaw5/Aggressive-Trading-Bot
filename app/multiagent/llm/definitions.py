"""Load `.claude/agents/*.md` as agent role definitions.

One source of truth, two consumers:

* **Claude Code** reads these files as subagent definitions (the frontmatter's
  `name`, `description` and `tools` are its schema).
* **This package** reads the same files and uses the markdown body as the system
  prompt for the Python-runtime agent.

The alternative — a prompt string in Python plus a markdown file describing the
same role — guarantees the two drift, and the drift is invisible until an agent
behaves differently depending on how it was invoked.
`tests/multiagent/test_agent_definitions.py` asserts each configured agent
resolves and carries the fields the runner needs.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class AgentDefinitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentDefinition:
    """A parsed role definition."""

    name: str
    description: str
    system_prompt: str
    path: Path
    tools: list[str] = field(default_factory=list)
    output_schema: str | None = None
    agent_key: str | None = None
    status: str = "active"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_wired(self) -> bool:
        return self.status != "defined_not_wired"


def _split_frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        raise AgentDefinitionError(
            f"{path} has no YAML frontmatter. Agent definitions need at least "
            "`name` and `description` so Claude Code and the Python runner agree "
            "on what the file is."
        )
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise AgentDefinitionError(f"{path} frontmatter is not terminated by a closing ---")
    meta = yaml.safe_load(parts[1]) or {}
    if not isinstance(meta, dict):
        raise AgentDefinitionError(f"{path} frontmatter must be a mapping")
    return meta, parts[2].strip()


def _parse_tools(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    return [t.strip() for t in str(raw).split(",") if t.strip()]


def load_definition(path: Path) -> AgentDefinition:
    if not path.exists():
        raise AgentDefinitionError(f"agent definition not found: {path}")
    meta, body = _split_frontmatter(path.read_text(encoding="utf-8"), path)

    for required in ("name", "description"):
        if not meta.get(required):
            raise AgentDefinitionError(f"{path} frontmatter is missing required key {required!r}")
    if not body:
        raise AgentDefinitionError(
            f"{path} has an empty body. The body IS the system prompt — an empty one "
            "would send the agent out with no role, no scope and no anti-hallucination rules."
        )

    known = {"name", "description", "tools", "output_schema", "agent_key", "status", "model"}
    return AgentDefinition(
        name=str(meta["name"]),
        description=str(meta["description"]),
        system_prompt=body,
        path=path,
        tools=_parse_tools(meta.get("tools")),
        output_schema=meta.get("output_schema"),
        agent_key=meta.get("agent_key"),
        status=str(meta.get("status", "active")),
        metadata={k: v for k, v in meta.items() if k not in known},
    )


@functools.lru_cache(maxsize=32)
def _load_cached(path_str: str) -> AgentDefinition:
    return load_definition(Path(path_str))


def load_agent(name: str, definitions_dir: Path) -> AgentDefinition:
    """Load by definition name (the filename stem)."""
    return _load_cached(str(definitions_dir / f"{name}.md"))


def load_all(definitions_dir: Path) -> dict[str, AgentDefinition]:
    if not definitions_dir.exists():
        raise AgentDefinitionError(f"agent definitions directory not found: {definitions_dir}")
    out: dict[str, AgentDefinition] = {}
    for p in sorted(definitions_dir.glob("*.md")):
        d = load_definition(p)
        out[d.name] = d
    return out


def clear_cache() -> None:
    _load_cached.cache_clear()
