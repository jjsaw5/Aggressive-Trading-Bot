"""Measurement wrappers — how "absent stays absent" becomes structural.

CLAUDE.md §4: *"Never substitute 0.0 for a missing measurement. A required float
plus an `or 0.0` fallback is how 67 of 67 audited signals came to report a spot
price of zero."*

The defence here is typing rather than discipline. A measurement is never a bare
float. It is a `Measurement`, which is either present (with a value, a source
and a timestamp) or absent (with a stated reason). Scoring rules take
`Measurement` objects, and a rule handed an absent one **abstains** — its points
leave the denominator instead of scoring zero.

`Measurement.value` is typed `float | None`, so a caller that reaches for the
number without checking gets a `TypeError` on arithmetic rather than a silent
zero. That is the intended failure mode.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AbsenceReason(str, Enum):
    """Why a value is missing. The distinction is load-bearing.

    Mirrors the export sentinels in CLAUDE.md §4: `NA_not_implemented` (no such
    concept), `NA_no_data` (concept exists, this row lacks it),
    `NA_unresolved`.
    """

    NOT_IMPLEMENTED = "NA_not_implemented"
    NO_DATA = "NA_no_data"
    UNRESOLVED = "NA_unresolved"
    PROVIDER_ERROR = "NA_provider_error"
    STALE = "NA_stale"


class Provenance(str, Enum):
    """Where a number came from. `MODELED` is never silently presentable.

    CLAUDE.md §4: *"Modeled is labeled."* Greeks computed by Black-Scholes carry
    MODELED and every surface that shows them says so.
    """

    PROVIDER = "provider"
    DERIVED = "derived"      # computed from provider data by our own code
    MODELED = "modeled"      # Black-Scholes and friends; not observed
    AGENT = "agent"          # an LLM said so; never used in scoring


class Measurement(BaseModel):
    """A single number, or an explicit statement that there isn't one."""

    name: str
    value: float | None = None
    unit: str = ""
    provenance: Provenance = Provenance.PROVIDER
    source: str | None = None
    as_of: datetime | None = None
    absence_reason: AbsenceReason | None = None
    note: str = ""

    @property
    def present(self) -> bool:
        return self.value is not None

    def require(self) -> float:
        """Value, or raise. For call sites where absence is a programming error."""
        if self.value is None:
            raise ValueError(
                f"measurement {self.name!r} is absent ({self.absence_reason or 'unknown'}); "
                "callers must branch on .present rather than defaulting it"
            )
        return self.value

    def export(self) -> float | str:
        """Value for an export row, or the sentinel string. Never a blank."""
        if self.value is None:
            return (self.absence_reason or AbsenceReason.NO_DATA).value
        return self.value

    @classmethod
    def of(
        cls,
        name: str,
        value: float | None,
        *,
        unit: str = "",
        provenance: Provenance = Provenance.PROVIDER,
        source: str | None = None,
        as_of: datetime | None = None,
        reason: AbsenceReason = AbsenceReason.NO_DATA,
        note: str = "",
    ) -> Measurement:
        """Build from a possibly-None value without an `or 0.0` anywhere."""
        return cls(
            name=name,
            value=value,
            unit=unit,
            provenance=provenance,
            source=source,
            as_of=as_of,
            absence_reason=None if value is not None else reason,
            note=note,
        )

    @classmethod
    def absent(
        cls,
        name: str,
        reason: AbsenceReason = AbsenceReason.NO_DATA,
        *,
        note: str = "",
        unit: str = "",
    ) -> Measurement:
        return cls(name=name, value=None, unit=unit, absence_reason=reason, note=note)


class MeasurementSet(BaseModel):
    """A named bag of measurements with a coverage figure.

    Coverage is reported everywhere a score is reported: a 78 computed from four
    of nine inputs is a materially different statement from a 78 computed from
    nine of nine, and the report must never present them identically.
    """

    measurements: dict[str, Measurement] = Field(default_factory=dict)

    def add(self, m: Measurement) -> Measurement:
        self.measurements[m.name] = m
        return m

    def add_many(self, ms: Iterable[Measurement]) -> None:
        for m in ms:
            self.add(m)

    def get(self, name: str) -> Measurement:
        """Always returns a Measurement — an unknown name is absent, not KeyError."""
        return self.measurements.get(
            name,
            Measurement.absent(name, AbsenceReason.NOT_IMPLEMENTED, note="never computed"),
        )

    def value(self, name: str) -> float | None:
        return self.get(name).value

    def present_count(self) -> int:
        return sum(1 for m in self.measurements.values() if m.present)

    def coverage(self) -> float | None:
        """Fraction of measurements that have a value, or None if the set is empty."""
        if not self.measurements:
            return None
        return self.present_count() / len(self.measurements)

    def absent_names(self) -> list[str]:
        return sorted(n for n, m in self.measurements.items() if not m.present)

    def export(self) -> dict[str, Any]:
        return {n: m.export() for n, m in self.measurements.items()}
