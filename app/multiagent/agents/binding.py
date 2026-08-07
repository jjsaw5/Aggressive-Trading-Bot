"""Evidence binding — the single chokepoint every agent output passes through.

An agent returns claims carrying `evidence_refs`. This module resolves each ref
against the run's ledger and:

* keeps claims whose refs resolve,
* **drops** claims whose refs are all unknown,
* strips unknown refs from claims that also carry known ones,
* records every drop as a `DataQualityRecord` and a line on the agent's run
  record.

The effect is that an agent can select and reason over retrieved material and
cannot introduce any. A model that invents a plausible headline with a plausible
id gets that claim silently removed from the brief and loudly recorded in the
audit trail. `tests/multiagent/test_evidence_binding.py` proves it with a runner
that deliberately lies.

The same code runs for the deterministic runner and for a live model, so this
path is exercised by every test in the suite rather than only when a key is set.
"""

from __future__ import annotations

from collections.abc import Iterable, MutableMapping
from datetime import datetime
from typing import Any

from app.logging_config import get_logger
from app.multiagent.models.enums import DataQualityFlag
from app.multiagent.models.evidence import EvidenceLedger
from app.multiagent.models.runs import DataQualityRecord

log = get_logger(__name__)


class BindingResult:
    """Outcome of binding one agent payload."""

    def __init__(self) -> None:
        self.dropped: list[str] = []
        self.stripped_refs: list[str] = []
        self.quality: list[DataQualityRecord] = []

    @property
    def clean(self) -> bool:
        return not self.dropped and not self.stripped_refs

    def summary(self) -> str:
        if self.clean:
            return "all agent claims resolved against the evidence ledger"
        return (
            f"{len(self.dropped)} claim(s) dropped for unresolvable evidence; "
            f"{len(self.stripped_refs)} stray reference(s) stripped"
        )


def bind_claims(
    claims: Iterable[MutableMapping[str, Any]],
    ledger: EvidenceLedger,
    *,
    ref_field: str = "evidence_refs",
    label: str = "claim",
    now: datetime,
    require_refs: bool = True,
    result: BindingResult | None = None,
) -> list[MutableMapping[str, Any]]:
    """Filter a list of claim dicts down to those backed by real evidence.

    `require_refs=False` is for claim types where an empty ref list is
    legitimate (a purely interpretive summary, say). It still strips unknown
    refs; it just does not drop the claim for having none.
    """
    res = result if result is not None else BindingResult()
    kept: list[MutableMapping[str, Any]] = []

    for claim in claims:
        raw = claim.get(ref_field) or []
        refs = [str(r) for r in raw] if isinstance(raw, list) else [str(raw)]
        resolved, unresolved = ledger.partition_refs(refs)

        if unresolved:
            res.stripped_refs.extend(unresolved)
            for ref in unresolved:
                res.quality.append(
                    DataQualityRecord(
                        flag=DataQualityFlag.UNREFERENCED_AGENT_CLAIM,
                        subject=ref,
                        detail=f"{label} cited evidence id {ref!r}, which is not in the ledger",
                        observed_at=now,
                    )
                )

        if require_refs and not resolved:
            descriptor = _describe(claim)
            res.dropped.append(f"{label}: {descriptor}")
            res.quality.append(
                DataQualityRecord(
                    flag=DataQualityFlag.UNREFERENCED_AGENT_CLAIM,
                    subject=descriptor,
                    detail=(
                        f"{label} dropped: no cited evidence resolved. An agent may only cite "
                        "ids the collector minted from retrieved data."
                    ),
                    observed_at=now,
                )
            )
            log.warning(
                "multiagent_claim_dropped",
                run_id=ledger.run_id,
                label=label,
                descriptor=descriptor[:120],
                unresolved=len(unresolved),
            )
            continue

        claim[ref_field] = resolved
        kept.append(claim)

    return kept


def restrict_to_known_symbols(
    claims: Iterable[MutableMapping[str, Any]],
    allowed: set[str],
    *,
    field: str = "ticker",
    label: str = "claim",
    now: datetime,
    result: BindingResult | None = None,
) -> list[MutableMapping[str, Any]]:
    """Drop claims about tickers this run has no data for.

    A candidate on a symbol nothing was retrieved for cannot be validated, so
    proposing it wastes a slot and, worse, produces a recommendation with no
    measurements behind it.
    """
    res = result if result is not None else BindingResult()
    kept: list[MutableMapping[str, Any]] = []
    for claim in claims:
        ticker = str(claim.get(field, "")).upper()
        if ticker and ticker in allowed:
            kept.append(claim)
            continue
        res.dropped.append(f"{label}: {ticker or '(no ticker)'} is outside the retrieved universe")
        res.quality.append(
            DataQualityRecord(
                flag=DataQualityFlag.OUT_OF_UNIVERSE_TICKER,
                subject=ticker or "(no ticker)",
                detail=(
                    f"{label} names a ticker with no retrieved market data; it cannot be "
                    "validated and was dropped"
                ),
                observed_at=now,
            )
        )
    return kept


def _describe(claim: MutableMapping[str, Any]) -> str:
    for key in ("headline", "ticker", "name", "primary_catalyst", "summary"):
        value = claim.get(key)
        if value:
            return str(value)[:160]
    return "(unlabelled claim)"
