"""Configuration and agent definitions.

Two things are checked here that are easy to get silently wrong:

* the methodology file is the only home for thresholds, so a constant that has
  crept back into code is a defect;
* `.claude/agents/*.md` are consumed by both Claude Code and the Python runtime,
  so a definition missing a field breaks one consumer without the other
  noticing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.multiagent.config import (
    CATEGORY_ORDER,
    MethodologyConfig,
    get_methodology,
    load_methodology,
)
from app.multiagent.llm import build_runner
from app.multiagent.llm.definitions import (
    AgentDefinitionError,
    load_agent,
    load_all,
    load_definition,
)
from app.multiagent.llm.runner import AgentRunnerError

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "app" / "multiagent"


# --- methodology ------------------------------------------------------------


def test_the_methodology_file_loads_and_is_versioned(methodology):
    assert methodology.version
    assert methodology.source_path.endswith("methodology.yaml")


def test_a_missing_methodology_file_raises_rather_than_defaulting():
    """There are deliberately no built-in scoring defaults."""
    with pytest.raises(FileNotFoundError, match="no built-in defaults"):
        load_methodology("/nonexistent/methodology.yaml")


def test_weights_that_do_not_total_100_are_rejected(tmp_path):
    raw = (REPO / "config" / "methodology.yaml").read_text()
    broken = raw.replace("technical_setup: 20", "technical_setup: 25", 1)
    path = tmp_path / "broken.yaml"
    path.write_text(broken)
    with pytest.raises(Exception, match="must sum to 100"):
        load_methodology(path)


def test_classification_bands_must_descend(tmp_path):
    raw = (REPO / "config" / "methodology.yaml").read_text()
    broken = raw.replace(
        '- { min: 90.0, label: "EXCEPTIONAL",     name: "Exceptional" }',
        '- { min: 10.0, label: "EXCEPTIONAL",     name: "Exceptional" }',
        1,
    )
    path = tmp_path / "bands.yaml"
    path.write_text(broken)
    with pytest.raises(Exception, match="high to low"):
        load_methodology(path)


def test_every_scoring_category_has_a_rule_block(methodology):
    for category in CATEGORY_ORDER:
        assert methodology.scoring.rules_for(category) is not None
        assert methodology.scoring.weights.for_category(category) > 0


def test_only_the_four_allowed_strategies_are_configured(methodology):
    assert set(methodology.strategies.allowed) == {
        "long_call",
        "long_put",
        "bull_call_spread",
        "bear_put_spread",
    }


def test_no_naked_or_undefined_risk_strategy_is_configurable(methodology):
    forbidden = {
        "bull_put_spread",
        "bear_call_spread",
        "long_straddle",
        "long_strangle",
        "iron_condor",
    }
    assert not (set(methodology.strategies.allowed) & forbidden)


def test_the_candidate_cap_matches_the_specification(methodology):
    assert methodology.run.max_candidates == 10


def test_an_alternate_methodology_file_can_be_loaded(tmp_path):
    raw = (REPO / "config" / "methodology.yaml").read_text()
    tweaked = raw.replace("min_score_to_rank: 60.0", "min_score_to_rank: 42.0", 1)
    path = tmp_path / "alt.yaml"
    path.write_text(tweaked)
    cfg = load_methodology(path)
    assert cfg.run.min_score_to_rank == 42.0
    # The default is untouched.
    assert get_methodology().run.min_score_to_rank == 60.0


def test_the_methodology_config_is_fully_typed():
    """Every field the engine reads is declared, so a missing key fails at load."""
    assert issubclass(MethodologyConfig, __import__("pydantic").BaseModel)


# --- constants do not live in code ------------------------------------------

# Numbers that are legitimately code, not methodology: array indices, unit
# conversions, percentages-to-fractions, rounding precision, and the documented
# scenario/rate constants that carry their own explanation.
_ALLOWED_LITERALS = {
    "0", "1", "2", "3", "4", "5", "10", "100", "1000", "365", "24", "60",
    "0.0", "1.0", "2.0", "0.5", "100.0", "365.0", "86400.0", "1000.0",
    "0.04",  # risk-free rate, documented at its definition
    "10.0",  # IV-crush scenario points, documented at its definition
}
_SCORING_FILES = ("engine.py", "components.py", "rules.py")


def test_the_scoring_engine_reads_thresholds_from_config_not_from_literals():
    """A threshold in code is a threshold nobody can review."""
    offenders: list[str] = []
    for name in _SCORING_FILES:
        path = APP / "scoring" / name
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or '"""' in stripped:
                continue
            # Look for comparisons against bare numeric literals.
            for match in re.finditer(r"(?:threshold|low|high|floor)=([0-9.]+)", stripped):
                literal = match.group(1)
                if literal not in _ALLOWED_LITERALS and "cfg." not in stripped:
                    offenders.append(f"{name}:{lineno}: {stripped[:100]}")
    assert not offenders, (
        "scoring thresholds must come from config/methodology.yaml:\n" + "\n".join(offenders)
    )


# --- agent definitions ------------------------------------------------------

AGENTS = ["market-intelligence", "opportunity-generator", "trade-validator", "risk-reviewer"]


@pytest.mark.parametrize("name", AGENTS)
def test_every_agent_definition_parses(name, methodology):
    definition = load_agent(name, methodology.definitions_path())
    assert definition.name == name
    assert definition.description
    assert definition.system_prompt
    assert definition.agent_key


@pytest.mark.parametrize("name", AGENTS)
def test_every_definition_states_its_role_scope_and_non_responsibilities(name, methodology):
    body = load_agent(name, methodology.definitions_path()).system_prompt.lower()
    assert "non-responsibilities" in body
    assert "hallucination" in body or "never fabricate" in body
    assert "order" in body  # each one states it cannot place orders


@pytest.mark.parametrize("name", AGENTS)
def test_no_agent_is_given_an_execution_tool(name, methodology):
    definition = load_agent(name, methodology.definitions_path())
    forbidden = {"bash", "write", "edit", "place_order", "submit_order"}
    assert not ({t.lower() for t in definition.tools} & forbidden), (
        f"{name} declares a tool that could place an order or mutate state"
    )


def test_the_three_pipeline_agents_are_wired_and_the_risk_reviewer_is_not(methodology):
    definitions = load_all(methodology.definitions_path())
    for name in ("market-intelligence", "opportunity-generator", "trade-validator"):
        assert definitions[name].is_wired
    # Defined so the interface is settled; deliberately not in the pipeline.
    assert not definitions["risk-reviewer"].is_wired


def test_the_configured_agent_names_all_resolve(methodology):
    for name in (
        methodology.agents.market_intelligence,
        methodology.agents.opportunity_generator,
        methodology.agents.trade_validator,
        methodology.agents.risk_reviewer,
    ):
        assert load_agent(name, methodology.definitions_path())


def test_a_definition_without_frontmatter_is_rejected(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text("# just a heading\n")
    with pytest.raises(AgentDefinitionError, match="frontmatter"):
        load_definition(path)


def test_a_definition_with_an_empty_body_is_rejected(tmp_path):
    """The body IS the system prompt; an empty one sends the agent out with no rules."""
    path = tmp_path / "empty.md"
    path.write_text("---\nname: x\ndescription: y\n---\n")
    with pytest.raises(AgentDefinitionError, match="empty body"):
        load_definition(path)


def test_a_definition_missing_a_required_key_is_rejected(tmp_path):
    path = tmp_path / "partial.md"
    path.write_text("---\nname: x\n---\nbody\n")
    with pytest.raises(AgentDefinitionError, match="description"):
        load_definition(path)


def test_agent2_is_told_not_to_state_prices_or_strikes(methodology):
    """Agent 2 has no live option data; a price from it would be invented."""
    body = load_agent("opportunity-generator", methodology.definitions_path()).system_prompt
    assert "Never state a price, a strike" in body


def test_agent3_is_told_not_to_produce_numbers(methodology):
    body = load_agent("trade-validator", methodology.definitions_path()).system_prompt
    assert "Never produce a number" in body


def test_the_risk_reviewer_definition_forbids_raising_a_score(methodology):
    """The asymmetry that keeps the deterministic score reproducible."""
    body = load_agent("risk-reviewer", methodology.definitions_path()).system_prompt
    assert "cannot raise a score" in body
    assert "cannot clear a hard rejection" in body


# --- runners ----------------------------------------------------------------


def test_the_deterministic_runner_is_the_default():
    assert build_runner().runner_id == "deterministic"


def test_an_unknown_runner_raises_rather_than_falling_back():
    """A run whose stated author is wrong is worse than a run that did not happen."""
    with pytest.raises(AgentRunnerError, match="unknown agent runner"):
        build_runner("gpt-9")


def test_the_anthropic_runner_fails_loudly_without_a_key(monkeypatch):
    pytest.importorskip("anthropic", reason="the llm extra is not installed")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from app.multiagent.llm.anthropic_runner import AnthropicAgentRunner

    with pytest.raises(AgentRunnerError, match="ANTHROPIC_API_KEY"):
        AnthropicAgentRunner(api_key=None)


def test_the_anthropic_runner_module_imports_without_the_sdk():
    """The optional dependency must not break the default install."""
    import importlib

    module = importlib.import_module("app.multiagent.llm.anthropic_runner")
    assert hasattr(module, "AnthropicAgentRunner")
