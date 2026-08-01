"""Pre-scoring rejections: setups that must never reach a candidate at all.

Distinct from `risk.evaluate_entry_gates`, which decides whether an already-built
candidate may be entered NOW. These run earlier and answer a different question:
should this setup produce a scored row in the research record at all?

Both current rules exist because the audit of build 7afa098 found the record
polluted in ways no amount of downstream analysis can repair:

  * An earnings report on or before expiry makes the trade an event binary
    (IV crush + gap), not the continuation the strategy claims. The system
    already detected this and wrote it into thesis prose — advisory text that
    gated nothing. An AAPL call spread was picked #1 the day before earnings on
    exactly that basis, and expired worthless on the print. A warning that never
    blocks is a warning nobody acts on.

  * 0DTE rows written without stored NBBO and intraday marks are uninterpretable:
    every one of the 38 audited 0DTE signals resolved `expiry` because daily
    marks cannot see an intraday target, so the managed exit the strategy claims
    to run was never actually measured. Collecting more of them adds rows, not
    information.

Rejections are LOGGED, not silently dropped — a setup that was seen and excluded
is evidence, and the directive requires it be visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.config import settings
from app.domain.enums import DTECategory, RejectReason
from app.logging_config import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class CaptureRejection:
    reason: RejectReason
    detail: str


def earnings_before_expiry(
    next_earnings: date | None, expiration: date | None
) -> CaptureRejection | None:
    """Reject when earnings land on or before the expiry being traded.

    Inclusive of the expiry date itself: a report on expiration day resolves the
    position on the print, which is the case the rule exists for."""
    if not settings.capture_earnings_hard_gate:
        return None
    if next_earnings is None or expiration is None:
        return None
    if next_earnings <= expiration:
        return CaptureRejection(
            RejectReason.EARNINGS_GATE,
            f"earnings {next_earnings} on/before expiry {expiration} — event binary "
            f"(IV crush + gap), not a continuation trade",
        )
    return None


def bucket_suspended(dte: DTECategory) -> CaptureRejection | None:
    """Reject every setup in a bucket whose data-capture dependencies are unmet."""
    suspended = {s.strip() for s in settings.capture_suspended_buckets.split(",") if s.strip()}
    if dte.value in suspended:
        return CaptureRejection(
            RejectReason.BUCKET_SUSPENDED,
            f"{dte.value} capture suspended pending NBBO persistence + intraday "
            f"marks; rows without them cannot be graded on the policy they claim",
        )
    return None


def evaluate_capture_gates(
    *, dte: DTECategory, symbol: str, next_earnings: date | None, expiration: date | None
) -> CaptureRejection | None:
    """First blocking rejection, or None. Logged either way when it blocks."""
    for rejection in (
        bucket_suspended(dte),
        earnings_before_expiry(next_earnings, expiration),
    ):
        if rejection is not None:
            log.info(
                "capture_gate_rejected",
                symbol=symbol,
                dte=dte.value,
                reason=rejection.reason.value,
                detail=rejection.detail,
            )
            return rejection
    return None
