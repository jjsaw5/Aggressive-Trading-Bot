"""Provider resolution for the multi-agent subsystem.

This package adds **no new vendor integrations**. The platform already has a
capability abstraction (`app.providers.base`) with FMP, Unusual Whales,
Robinhood, Benzinga and a mock stack behind it, and duplicating that would be
the worst possible outcome. `resolve()` delegates to the existing registry.

The one local decision: which news/economic-calendar implementation answers when
the registry routes to `mock`. See `mock_research.ResearchMockProvider` for why.
"""

from __future__ import annotations

from typing import Any

from app.config import ProviderName, settings
from app.multiagent.runtime import get_runtime
from app.providers import registry


def _research_mock_active(configured: str | ProviderName) -> bool:
    value = configured.value if isinstance(configured, ProviderName) else str(configured)
    return get_runtime().ma_use_research_mock and value == ProviderName.MOCK.value


def news_provider() -> Any:
    """News source, preferring the richer synthetic corpus when mocked."""
    if _research_mock_active(settings.provider_news):
        from app.multiagent.providers.mock_research import ResearchMockProvider

        return ResearchMockProvider()
    return registry.news_provider()


def economic_calendar_provider() -> Any:
    if _research_mock_active(settings.provider_econ_calendar):
        from app.multiagent.providers.mock_research import ResearchMockProvider

        return ResearchMockProvider()
    return registry.econ_calendar_provider()


__all__ = ["economic_calendar_provider", "news_provider"]
