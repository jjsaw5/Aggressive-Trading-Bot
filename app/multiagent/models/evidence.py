"""The evidence ledger — the control that stops an agent inventing market data.

The pipeline's stated principle is:

    AI generates hypotheses. APIs provide evidence. Code validates and scores.

That only holds if "evidence" is a thing the code owns. So: **Python retrieves
first and assigns ids**. An agent is shown the ledger and may cite ids from it.
Every claim an agent returns carries `evidence_refs`. On the way back, each ref
is checked against the ledger:

* ref resolves          -> the claim is *bound* to a real retrieved artifact
* ref does not resolve  -> the claim is DROPPED and a
                           `UNREFERENCED_AGENT_CLAIM` quality flag is recorded

An agent therefore cannot introduce a headline, a date, or a number that no
provider returned. It can only select among, and reason about, what was
actually fetched. This is checked in `tests/multiagent/test_evidence_binding.py`
with a deliberately lying agent.

The ledger is also the provenance record. Every item keeps source, url, and both
`published_at` and `retrieved_at`, so a stored recommendation can be replayed
against exactly the material that produced it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.multiagent.models.enums import EvidenceKind, EvidenceQuality


def _stable_id(kind: EvidenceKind, *parts: Any) -> str:
    """Deterministic short id.

    Deterministic rather than random so re-running a scan over identical inputs
    yields identical ids — which makes a stored run diffable and a test able to
    assert on a specific reference.
    """
    payload = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]
    return f"{kind.value[:4]}-{digest}"


class EvidenceItem(BaseModel):
    """One retrieved artifact, addressable by id.

    `summary` is what the agent sees. `payload` is the structured original, kept
    so scoring reads the real object rather than a prose rendering of it.
    """

    id: str
    kind: EvidenceKind
    symbol: str | None = None

    # Provenance. `source` is the provider or outlet; `url` the primary link.
    source: str
    url: str | None = None
    headline: str | None = None
    summary: str = ""

    # Both timestamps, always. published_at may be genuinely unknown (None);
    # retrieved_at never is.
    published_at: datetime | None = None
    retrieved_at: datetime

    quality: EvidenceQuality = EvidenceQuality.REPORTED
    payload: dict[str, Any] = Field(default_factory=dict)

    def age_days(self, now: datetime) -> float | None:
        """Age in days, or None when publication time is unknown.

        Returns None rather than assuming `retrieved_at`: substituting the
        retrieval time for an unknown publication time would make every
        undated item look fresh.
        """
        if self.published_at is None:
            return None
        return (now - self.published_at).total_seconds() / 86400.0

    def render(self) -> str:
        """One line, as shown to an agent. The id leads so citing is easy."""
        when = self.published_at.isoformat() if self.published_at else "published_at=NA_no_data"
        head = self.headline or self.summary or self.kind.value
        sym = f" [{self.symbol}]" if self.symbol else ""
        src = f" ({self.source}{', ' + self.url if self.url else ''})"
        return f"{self.id}{sym} {when} {head}{src}"


class EvidenceLedger(BaseModel):
    """The set of artifacts an agent is permitted to cite in one run."""

    run_id: str
    built_at: datetime
    items: dict[str, EvidenceItem] = Field(default_factory=dict)

    # Providers that were asked but returned nothing or errored. Kept because a
    # gap is information: "no news found" and "the news provider was down" lead
    # to different conclusions, and the report says which happened.
    provider_errors: dict[str, str] = Field(default_factory=dict)

    def add(self, item: EvidenceItem) -> EvidenceItem:
        self.items[item.id] = item
        return item

    def add_many(self, items: Iterable[EvidenceItem]) -> None:
        for item in items:
            self.add(item)

    def get(self, ref: str) -> EvidenceItem | None:
        return self.items.get(ref)

    def __contains__(self, ref: object) -> bool:
        return isinstance(ref, str) and ref in self.items

    def __len__(self) -> int:
        return len(self.items)

    def of_kind(self, *kinds: EvidenceKind) -> list[EvidenceItem]:
        wanted = set(kinds)
        return [i for i in self.items.values() if i.kind in wanted]

    def narrative_items(self) -> list[EvidenceItem]:
        """Evidence that can carry a catalyst: news and dated company events.

        Excludes quotes, chains and flow — those are measurements, and treating
        a quote as a catalyst is how "the stock went up" becomes a reason.
        """
        return self.of_kind(
            EvidenceKind.NEWS, EvidenceKind.EARNINGS_EVENT, EvidenceKind.CALENDAR_CATALYST
        )

    def economic_items(self) -> list[EvidenceItem]:
        return self.of_kind(EvidenceKind.ECONOMIC_EVENT)

    def for_symbol(self, symbol: str) -> list[EvidenceItem]:
        s = symbol.upper()
        return [i for i in self.items.values() if (i.symbol or "").upper() == s]

    def symbols(self) -> set[str]:
        return {i.symbol.upper() for i in self.items.values() if i.symbol}

    def partition_refs(self, refs: Sequence[str]) -> tuple[list[str], list[str]]:
        """Split a claim's references into (resolved, unresolved).

        The single chokepoint every agent output passes through.
        """
        resolved = [r for r in refs if r in self.items]
        unresolved = [r for r in refs if r not in self.items]
        return resolved, unresolved

    def render(self, limit: int | None = None, kinds: Sequence[EvidenceKind] | None = None) -> str:
        """The ledger as an agent sees it: one id-led line per item."""
        items = list(self.items.values())
        if kinds:
            wanted = set(kinds)
            items = [i for i in items if i.kind in wanted]
        # Most recent first, undated last — an agent reading top-down should
        # meet the freshest material first.
        items.sort(key=lambda i: (i.published_at is None, -(i.published_at or i.retrieved_at).timestamp()))
        if limit is not None:
            items = items[:limit]
        if not items:
            return "(no evidence retrieved)"
        return "\n".join(i.render() for i in items)


def make_evidence_id(kind: EvidenceKind, *parts: Any) -> str:
    """Public helper so collectors mint ids the same way the ledger expects."""
    return _stable_id(kind, *parts)
