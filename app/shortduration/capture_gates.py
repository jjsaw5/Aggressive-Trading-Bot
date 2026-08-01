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
    """Reject every setup in a bucket whose data-capture dependencies are unmet.

    0DTE's re-enable conditions are QUANTITATIVE as of reviewer Ruling 2, and no
    longer merely "intraday marks ship". Intraday marks did ship (Phase 2) and
    0DTE stays suspended anyway, because the marks that shipped are trade-driven
    and sparse: 31% session coverage with a 53-minute maximum gap on a LIQUID
    contract. For a bucket whose entire trade lives inside one session, a
    53-minute blind spot is not a bound on the answer — it is a hole in it.

    The bar is now three conditions, all required:
      1. stored NBBO at signal time                     — DONE (Phase 1)
      2. intraday marks                                 — DONE (Phase 2)
      3. demonstrated coverage >= 80% of RTH minutes
         with max gap <= 5 minutes, on a representative
         contract sample                                — NOT MET
    See app/analytics/mark_quality.py for (3)'s measurement and thresholds.
    """
    suspended = {s.strip() for s in settings.capture_suspended_buckets.split(",") if s.strip()}
    if dte.value in suspended:
        from app.analytics.mark_quality import (
            ZERO_DTE_MAX_GAP_MINUTES,
            ZERO_DTE_MIN_COVERAGE_PCT,
        )

        detail = (
            f"{dte.value} capture suspended pending NBBO persistence + intraday "
            f"marks; rows without them cannot be graded on the policy they claim"
        )
        if dte == DTECategory.ZERO_DTE:
            detail = (
                f"{dte.value} capture suspended: NBBO (done) + intraday marks "
                f"(done) + demonstrated coverage >= "
                f"{ZERO_DTE_MIN_COVERAGE_PCT:.0%} of RTH with max gap <= "
                f"{ZERO_DTE_MAX_GAP_MINUTES}min (NOT MET — measured 31% coverage, "
                f"52min max gap on a liquid contract). A same-session trade "
                f"cannot be graded through a 52-minute blind spot."
            )
        return CaptureRejection(RejectReason.BUCKET_SUSPENDED, detail)
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
