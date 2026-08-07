"""Catalyst validation — does the reason for the trade survive checking?

Five questions, answered from retrieved evidence rather than from the agent's
own description of it:

1. **Does it exist?** Do the candidate's `evidence_refs` resolve in the ledger?
2. **Is it recent?** Age of the newest supporting item.
3. **Is it material?** Evidence quality plus how many independent items back it.
4. **Has it been priced in?** How far the underlying has already travelled since
   the catalyst published.
5. **Does the timing work, and does anything collide with it?** Whether the
   event lands inside the expected hold, and which scheduled macro events
   overlap.

The priced-in check is the one worth stating plainly. It compares the move since
publication against a configured threshold. That is a **heuristic**, not a
measurement of information absorption — a stock can move 8% on a catalyst and
still have further to run, and it can move 1% while fully repricing. The rule
carries a small penalty rather than a rejection for exactly that reason.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.domain.market import PriceHistory
from app.multiagent.models.candidates import ResearchCandidate
from app.multiagent.models.enums import (
    EvidenceKind,
    EvidenceQuality,
    TimeHorizon,
    ValidationVerdict,
)
from app.multiagent.models.evidence import EvidenceLedger
from app.multiagent.models.validation import CatalystValidation

# Days each horizon is taken to mean, for the "does the catalyst land inside the
# hold" test. Coarse by design — the horizons are themselves coarse buckets.
HORIZON_DAYS: dict[TimeHorizon, int] = {
    TimeHorizon.INTRADAY: 1,
    TimeHorizon.ONE_TO_THREE_DAYS: 3,
    TimeHorizon.ONE_WEEK: 7,
    TimeHorizon.TWO_TO_FOUR_WEEKS: 28,
    TimeHorizon.ONE_TO_THREE_MONTHS: 90,
    TimeHorizon.UNKNOWN: 21,
}


def horizon_days(horizon: TimeHorizon) -> int:
    return HORIZON_DAYS.get(horizon, 21)


def _price_at_or_after(history: PriceHistory | None, when: datetime) -> float | None:
    """Close of the first bar at or after `when`, or None.

    Returns None rather than the nearest available bar when the history starts
    after the catalyst: a close from three weeks later is not "the price at the
    catalyst", and using it would silently answer a different question.
    """
    if history is None or not history.candles:
        return None
    target = when.date()
    for candle in history.candles:
        ts = candle.ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts.date() >= target:
            return candle.close
    return None


def validate_catalyst(
    candidate: ResearchCandidate,
    ledger: EvidenceLedger,
    *,
    now: datetime,
    history: PriceHistory | None,
    current_price: float | None,
    max_news_age_days: int,
    priced_in_move_pct: float,
    scheduled_macro: list[tuple[str, datetime]] | None = None,
) -> CatalystValidation:
    cv = CatalystValidation(
        ticker=candidate.ticker,
        as_of=now,
        claimed_catalyst=candidate.primary_catalyst,
    )

    refs = candidate.all_refs()
    resolved, unresolved = ledger.partition_refs(refs)
    cv.resolved_evidence_ids = resolved
    cv.unresolved_refs = unresolved
    cv.exists = bool(resolved)

    if unresolved:
        cv.notes.append(
            f"{len(unresolved)} evidence reference(s) did not resolve against the ledger "
            "and were discarded"
        )

    if not resolved:
        cv.verdict = ValidationVerdict.CONTRADICTS
        cv.notes.append(
            "no cited evidence resolves — the stated catalyst is unverifiable from anything "
            "this run retrieved"
        )
        return cv

    items = [ledger.get(r) for r in resolved]
    items = [i for i in items if i is not None]

    # Evidence quality: the best of what backs it.
    order = {
        EvidenceQuality.CONFIRMED_FACT: 3,
        EvidenceQuality.REPORTED: 2,
        EvidenceQuality.INTERPRETATION: 1,
        EvidenceQuality.SPECULATION: 0,
    }
    cv.evidence_quality = max((i.quality for i in items), key=lambda q: order[q])

    # Age of the newest dated item. Undated items do not count as fresh.
    ages = [a for a in (i.age_days(now) for i in items) if a is not None]
    cv.newest_evidence_age_days = round(min(ages), 2) if ages else None
    if cv.newest_evidence_age_days is None:
        cv.notes.append(
            "no supporting item carries a publication time — freshness is unknown, not fresh"
        )

    # Scheduled events among the evidence.
    scheduled_dates: list[date] = []
    for i in items:
        if i.kind in (EvidenceKind.EARNINGS_EVENT, EvidenceKind.CALENDAR_CATALYST, EvidenceKind.ECONOMIC_EVENT):
            raw = i.payload.get("report_date") or i.payload.get("event_date")
            if raw:
                try:
                    scheduled_dates.append(date.fromisoformat(str(raw)))
                except ValueError:
                    continue
    future = sorted(d for d in scheduled_dates if d >= now.date())
    if future:
        cv.is_scheduled = True
        cv.scheduled_date = future[0]
        hold = horizon_days(candidate.expected_holding_period)
        cv.within_expected_horizon = (future[0] - now.date()).days <= hold
        if not cv.within_expected_horizon:
            cv.notes.append(
                f"scheduled catalyst {future[0].isoformat()} falls outside the "
                f"{hold}-day expected hold"
            )
    elif cv.newest_evidence_age_days is not None:
        # An unscheduled catalyst is "timely" if it is recent enough to still matter.
        cv.within_expected_horizon = cv.newest_evidence_age_days <= max_news_age_days

    # Priced in? Compare the move since the catalyst against the threshold.
    dated = [i for i in items if i.published_at is not None]
    if dated and current_price is not None:
        newest = max(dated, key=lambda i: i.published_at)  # type: ignore[arg-type]
        ref_price = _price_at_or_after(history, newest.published_at)  # type: ignore[arg-type]
        if ref_price:
            move = (current_price - ref_price) / ref_price * 100.0
            cv.move_since_catalyst_pct = round(move, 3)
            # Only a move IN THE THESIS DIRECTION can price a catalyst in. A
            # stock that fell 8% since bullish news has not priced it in.
            with_thesis = move if candidate.is_bullish() else -move
            cv.likely_priced_in = with_thesis >= priced_in_move_pct
            if cv.likely_priced_in:
                cv.notes.append(
                    f"{candidate.ticker} has already moved {with_thesis:+.1f}% with the thesis "
                    f"since the catalyst published, past the {priced_in_move_pct:g}% "
                    "priced-in threshold (heuristic, not a measure of information absorption)"
                )
        else:
            cv.notes.append(
                "price history does not reach back to the catalyst — priced-in check abstains "
                "rather than substituting a later bar"
            )

    # Colliding scheduled events inside the hold.
    hold_days = horizon_days(candidate.expected_holding_period)
    horizon_end = now + timedelta(days=hold_days)
    for name, when in scheduled_macro or []:
        if now <= when <= horizon_end:
            cv.conflicting_events.append(f"{name} at {when.isoformat()}")

    # Verdict.
    stale = cv.newest_evidence_age_days is not None and cv.newest_evidence_age_days > max_news_age_days
    if cv.evidence_quality is EvidenceQuality.SPECULATION:
        cv.verdict = ValidationVerdict.CONTRADICTS
    elif stale and not cv.is_scheduled:
        cv.verdict = ValidationVerdict.MIXED
        cv.notes.append(
            f"newest supporting item is {cv.newest_evidence_age_days:.1f} days old, past the "
            f"{max_news_age_days}-day freshness bar"
        )
    elif cv.likely_priced_in:
        cv.verdict = ValidationVerdict.MIXED
    elif cv.within_expected_horizon is False:
        cv.verdict = ValidationVerdict.MIXED
    elif cv.evidence_quality in (EvidenceQuality.CONFIRMED_FACT, EvidenceQuality.REPORTED):
        cv.verdict = ValidationVerdict.CONFIRMS
    else:
        cv.verdict = ValidationVerdict.INSUFFICIENT_DATA
    return cv
