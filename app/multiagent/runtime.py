"""Runtime settings for the multi-agent subsystem.

Kept separate from `app.config.settings` on purpose. That object is read by the
frozen short-duration model's code path, and `CLAUDE.md` §2 treats a behaviour
change there as a capture-window event. Adding fields to it for an unrelated
subsystem invites exactly the kind of accidental coupling FINDING_01 was.

Split of responsibility:

* **`config/methodology.yaml`** — what a score *means*. Weights, thresholds,
  bands. Reviewable, versioned, stamped onto every stored score.
* **here (env)** — where data and judgement come from. Runner choice, model,
  credentials, persistence toggles. Changing one of these changes the run's
  provenance, never the meaning of a number.

Credentials are read from the environment only. `.env` is gitignored and no
credential is ever written to a prompt, a log, a report or a database row.
"""

from __future__ import annotations

import functools

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MultiAgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Agent execution ---------------------------------------------------
    # "deterministic" (no credentials) or "anthropic". Never falls back
    # silently: asking for anthropic without a key is an error, because a run
    # whose stated author is wrong is worse than a run that did not happen.
    ma_agent_runner: str = "deterministic"
    ma_anthropic_model: str = "claude-sonnet-5"
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    # --- Data --------------------------------------------------------------
    # When the news/econ providers route to `mock`, use the richer research
    # corpus in app/multiagent/providers/mock_research.py instead. The platform
    # mock's news is a single repeated headline — fine for the latency tests it
    # was written for, useless for exercising catalyst classification.
    # `app/providers/mock/provider.py` is a freeze-guarded path, so the richer
    # corpus lives beside this subsystem rather than being added there.
    ma_use_research_mock: bool = True

    # --- Persistence -------------------------------------------------------
    ma_persist: bool = True

    # --- Methodology -------------------------------------------------------
    # Override the methodology file (tests point this at a fixture).
    ma_methodology_path: str | None = None

    # --- Safety ------------------------------------------------------------
    # Present so the guarantee is a property of configuration rather than a
    # claim in a document. Nothing reads this to *enable* execution — there is
    # no order-placement code path in this subsystem at all. It is recorded on
    # every run so the corpus can demonstrate that.
    ma_execution_enabled: bool = False


@functools.lru_cache(maxsize=1)
def get_runtime() -> MultiAgentSettings:
    return MultiAgentSettings()


def clear_cache() -> None:
    get_runtime.cache_clear()
