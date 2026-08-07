"""Evidence retrieval: providers are called here, and only here, before agents run."""

from app.multiagent.evidence.collector import (
    SECTOR_PROXIES,
    EvidenceCollector,
    MarketEvidence,
    ProviderCallRecorder,
    build_index_context,
    sector_proxy_for,
    upcoming_earnings_within,
)

__all__ = [
    "SECTOR_PROXIES",
    "EvidenceCollector",
    "MarketEvidence",
    "ProviderCallRecorder",
    "build_index_context",
    "sector_proxy_for",
    "upcoming_earnings_within",
]
